from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

import app.local_analyzer as analyzer_module
from app.analyzer import AnalysisContext, InsufficientEvidenceError
from app.local_analyzer import AnalyzerConfig, QwenLocalAnalyzer
from app.ml.video import VideoAnalysisError
from app.ml.rules import RULES
from app.ml.model import PROMPT
from app.rules import load_rule_catalog
from ml_tools.annotations import match_events, validate_annotations


def summarize(rows: list[dict], tolerance_sec: float = 5) -> dict:
    counts = dict(tp=0, fp=0, tn=0, fn=0, failed=0)
    point_errors = []
    latencies = []
    for row in rows:
        latencies.append(row['elapsed_sec'])
        if row['status'] != 'done':
            counts['failed'] += 1
            continue
        truth = row['label'] == 'violation'
        positive = row['verdict'] == 'violation'
        counts['tp' if truth and positive else 'fn' if truth else 'fp' if positive else 'tn'] += 1
        if truth and positive and row.get('point_sec') is not None:
            point_errors.append(min(abs(t - row['point_sec']) for t in row['event_times']))
    n = len(rows)
    completed = n - counts['failed']
    def ratio(a, b):
        return round(a / b, 4) if b else None
    return {**counts, 'videos': n, 'coverage': ratio(completed, n),
            'accuracy_completed': ratio(counts['tp'] + counts['tn'], completed),
            'correct_over_all': ratio(counts['tp'] + counts['tn'], n),
            'precision_completed': ratio(counts['tp'], counts['tp'] + counts['fp']),
            'recall_completed': ratio(counts['tp'], counts['tp'] + counts['fn']),
            'elapsed_max_sec': max(latencies, default=0), 'elapsed_median_sec': statistics.median(latencies) if latencies else None,
            'point_matches_within_tolerance': sum(e <= tolerance_sec for e in point_errors),
            'point_predictions_scored': len(point_errors), 'point_tolerance_sec': tolerance_sec,
            'note': 'Binary labels only. Point distance does not measure interval/type accuracy or extra event false positives. Failures are reported separately.'}


class MemorySampler:
    def __init__(self):
        self.stop = threading.Event()
        self.peak = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                value = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'], timeout=3, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.peak = max(self.peak, int(value.decode().splitlines()[0]))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self.stop.wait(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, default=Path('.local_ml/dataset.json'))
    parser.add_argument('--mode', choices=['benchmark', 'dev', 'holdout', 'video-holdout'], default='benchmark')
    parser.add_argument('--output', type=Path, default=Path('.local_ml/evaluation'))
    parser.add_argument('--limit', type=int)
    parser.add_argument('--budget-sec', type=float, default=210)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8091')
    parser.add_argument('--frames', type=int, default=12)
    parser.add_argument('--window-sec', type=float, default=24)
    parser.add_argument('--stride-sec', type=float, default=18)
    parser.add_argument('--annotations', type=Path)
    parser.add_argument('--max-side', type=int, default=848)
    parser.add_argument('--rules', type=Path, default=Path('app/data/ml-rules.json'))
    parser.add_argument('--ids', nargs='+', help='Explicit subset for diagnostics; recorded in report')
    parser.add_argument('--model-lock', type=Path, default=Path('ml_tools/model35-lock.json'),
                        help='Declared runtime weights; does not verify the running server identity')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    annotations = json.loads(args.annotations.read_text(encoding='utf-8'))['annotations'] if args.annotations else {}
    if args.mode == 'holdout' and not annotations:
        raise SystemExit('Holdout requires reviewed annotations to revalidate source groups.')
    if annotations:
        validate_annotations(manifest, annotations, require_all=args.mode == 'holdout')
    if args.mode == 'holdout' and not manifest.get('source_grouping_reviewed'):
        raise SystemExit('Finalize source/camera/session grouping locally before evaluating holdout.')
    split = 'dev' if args.mode == 'benchmark' else 'holdout' if args.mode == 'video-holdout' else args.mode
    unique = {r['sha256']: r for r in manifest['records'] if r['split'] == split}
    selected = sorted(unique.values(), key=lambda r: r['duration_sec'])
    if args.ids:
        selected = [r for r in selected if r['id'] in args.ids]
    if not selected:
        raise SystemExit('Selected split has no eligible videos.')
    if args.mode == 'benchmark':
        selected = [selected[0], selected[-1]]
    if args.limit:
        selected = selected[:args.limit]
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit('Choose a fresh --output directory to preserve previous measurements.')
    args.output.mkdir(parents=True, exist_ok=True)
    cfg = AnalyzerConfig(endpoint=args.endpoint, budget_sec=args.budget_sec, frames_per_window=args.frames,
                         window_sec=args.window_sec, stride_sec=args.stride_sec, max_side=args.max_side)
    analyzer = QwenLocalAnalyzer(cfg)
    catalog = load_rule_catalog(args.rules)
    # Research measures candidate procedures without activating them in the API.
    # Predictions stay private and are not materialized as verified legal findings.
    research_rule_ids = tuple(rule.id for rule in catalog.rules)
    if not research_rule_ids or set(research_rule_ids) - set(RULES):
        raise SystemExit('Research catalog must contain supported candidate procedures')
    provenance = {
        'model_lock': json.loads(args.model_lock.read_text(encoding='utf-8')),
        'model_identity_verified': False,
        'prompt_sha256': hashlib.sha256(PROMPT.encode('utf-8')).hexdigest(),
        'catalog_sha256': hashlib.sha256(args.rules.read_bytes()).hexdigest(),
        'analyzer_sha256': hashlib.sha256(Path(analyzer_module.__file__).read_bytes()).hexdigest(),
    }
    rows = []
    event_totals = dict(tp=0, fp=0, fn=0, reviewed_videos=0)
    memory = MemorySampler()
    memory.thread.start()
    try:
        for item in selected:
            started = time.monotonic()
            row = {k: item[k] for k in ('id', 'label', 'point_sec', 'duration_sec', 'sha256')}
            try:
                with Path(item['path']).open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != item['sha256']:
                        raise VideoAnalysisError('Video changed after dataset audit')
                result, metrics = analyzer.analyze_with_metrics(AnalysisContext(job_id=item['id'], video_path=Path(item['path']),
                    duration_sec=item['duration_sec'], enabled_rule_ids=research_rule_ids))
                row.update(status='done', verdict=result.verdict, event_times=[e.timecode_sec for e in result.violations], **metrics)
                # Full observations stay in a private file and are never printed.
                (args.output / f"{item['id']}.result.json").write_text(result.model_dump_json(indent=2), encoding='utf-8')
                annotation = annotations.get(item['id'], {})
                if annotation.get('reviewed') and annotation.get('verdict') != 'uncertain':
                    predictions = [{'rule_id': e.rule_id, 'timecode_sec': e.timecode_sec} for e in result.violations]
                    matches = match_events(annotation['events'], predictions)
                    for key, value in matches.items():
                        event_totals[key] += value
                    event_totals['reviewed_videos'] += 1
            except (VideoAnalysisError, InsufficientEvidenceError) as error:
                row.update(status='failed', error=str(error), elapsed_sec=round(time.monotonic() - started, 3))
            rows.append(row)
            report = {'mode': args.mode, 'configuration': asdict(cfg), 'summary': summarize(rows),
                      'provenance': provenance,
                      'rule_ids': list(research_rule_ids), 'catalog_activation': 'research_only', 'explicit_ids': args.ids,
                      'evaluation_scope': 'Whole-video split with exact-duplicate grouping only; camera/session membership is unverified. Not an unseen-view evaluation.' if args.mode == 'video-holdout' else 'Development/diagnostic unless grouped holdout explicitly requested.',
                      'event_metrics_completed_only': event_totals if annotations else None,
                      'gpu_peak_used_mib': memory.peak, 'rows': rows}
            (args.output / 'metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: row[k] for k in ('id', 'status', 'elapsed_sec')}, ensure_ascii=False), flush=True)
        print(json.dumps({'summary': summarize(rows), 'gpu_peak_used_mib': memory.peak}, ensure_ascii=False), flush=True)
    finally:
        memory.stop.set()
        memory.thread.join(timeout=4)


if __name__ == '__main__':
    main()
