from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
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
from ml_tools.evaluation_lock import EvaluationLock, EvaluationLockError
from ml_tools.source_aliases import resolve_annotations_by_sha, select_unique_sources


def summarize(rows: list[dict], tolerance_sec: float = 5) -> dict:
    counts = dict(tp=0, fp=0, tn=0, fn=0, failed=0)
    failures = dict(abstained=0, technical_failed=0, failed_unclassified=0)
    failed_positives = 0
    point_errors = []
    latencies = []
    for row in rows:
        latencies.append(row['elapsed_sec'])
        if row['status'] != 'done':
            counts['failed'] += 1
            code = row.get('error_code')
            failures['abstained' if code == 'insufficient_evidence' else
                     'technical_failed' if code else 'failed_unclassified'] += 1
            failed_positives += row['label'] == 'violation'
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
    return {**counts, **failures, 'videos': n, 'coverage': ratio(completed, n),
            'accuracy_completed': ratio(counts['tp'] + counts['tn'], completed),
            'correct_over_all': ratio(counts['tp'] + counts['tn'], n),
            'precision_completed': ratio(counts['tp'], counts['tp'] + counts['fp']),
            'recall_completed': ratio(counts['tp'], counts['tp'] + counts['fn']),
            'recall_over_all_positives': ratio(counts['tp'], counts['tp'] + counts['fn'] + failed_positives),
            'positive_videos_without_supported_result': counts['fn'] + failed_positives,
            'elapsed_max_sec': max(latencies, default=0), 'elapsed_median_sec': statistics.median(latencies) if latencies else None,
            'point_matches_within_tolerance': sum(e <= tolerance_sec for e in point_errors),
            'point_predictions_scored': len(point_errors), 'point_tolerance_sec': tolerance_sec,
            'note': 'Binary labels only. Point distance does not measure interval/type accuracy or extra event false positives. Failures are reported separately.'}


def score_supported_events(annotation: dict, predictions: list[dict], rule_ids: tuple[str, ...]) -> dict | None:
    """Score validated human review only, within this run's requested catalog.

    OTHER remains a whole-task reference but is not a false negative of a
    detector that cannot emit OTHER. Predictions of the wrong procedure still
    count as false positives; a broad violation label is not a class match.
    """
    if annotation.get('reviewed') is not True or annotation.get('verdict') not in {'violation', 'no_violation'}:
        return None
    supported = set(rule_ids)
    return match_events([event for event in annotation['events'] if event['rule_id'] in supported], predictions)


def audit_reference_scope(selected: list[dict], annotations: dict, rule_ids: tuple[str, ...],
                          rows: list[dict], event_totals: dict | None = None) -> dict:
    """Aggregate annotation coverage without exposing source IDs or descriptions.

    Selected references include pending videos. Failure counts and recall cover
    processed, human-reviewed, non-uncertain references only. Draft/uncertain
    events never become ground truth through this bookkeeping.
    """
    counts = dict(selected_videos=len(selected), missing_annotation_videos=0,
                  draft_videos=0, draft_events=0,
                  reviewed_uncertain_videos=0, reviewed_uncertain_events=0,
                  reviewed_scorable_videos=0, supported_reference_events=0,
                  outside_catalog_reference_events=0, other_reference_events=0,
                  completed_reviewed_scorable_videos=0, failed_reviewed_scorable_videos=0,
                  pending_reviewed_scorable_videos=0,
                  supported_reference_events_on_completed_videos=0,
                  supported_reference_events_on_failed_videos=0,
                  outside_catalog_reference_events_on_failed_videos=0)
    statuses = {row['id']: row['status'] for row in rows}
    supported = set(rule_ids)
    for item in selected:
        annotation = annotations.get(item['id'])
        if annotation is None:
            counts['missing_annotation_videos'] += 1
            continue
        events = annotation.get('events', [])
        if annotation.get('reviewed') is not True:
            counts['draft_videos'] += 1
            counts['draft_events'] += len(events) if isinstance(events, list) else 0
            continue
        if annotation['verdict'] == 'uncertain':
            counts['reviewed_uncertain_videos'] += 1
            counts['reviewed_uncertain_events'] += len(events)
            continue
        counts['reviewed_scorable_videos'] += 1
        n_supported = sum(event['rule_id'] in supported for event in events)
        n_outside = len(events) - n_supported
        counts['supported_reference_events'] += n_supported
        counts['outside_catalog_reference_events'] += n_outside
        counts['other_reference_events'] += sum(event['rule_id'] == 'OTHER' for event in events)
        status = statuses.get(item['id'])
        if status == 'done':
            counts['completed_reviewed_scorable_videos'] += 1
            counts['supported_reference_events_on_completed_videos'] += n_supported
        elif status is not None:
            counts['failed_reviewed_scorable_videos'] += 1
            counts['supported_reference_events_on_failed_videos'] += n_supported
            counts['outside_catalog_reference_events_on_failed_videos'] += n_outside
        else:
            counts['pending_reviewed_scorable_videos'] += 1
    processed_supported = (counts['supported_reference_events_on_completed_videos']
                           + counts['supported_reference_events_on_failed_videos'])
    return {**counts, 'supported_rule_ids': list(rule_ids),
            'supported_event_recall_over_processed_scorable_references': (
                round(event_totals['tp'] / processed_supported, 4)
                if event_totals is not None and processed_supported else None),
            'notice': (
                'Event TP/FP/FN cover human-reviewed, non-uncertain references within the requested catalog only. '
                'OTHER and other out-of-catalog references are counted separately, not reclassified as correct negatives. '
                'Draft and uncertain events are not ground truth. Completed-only event metrics exclude failed videos; '
                'their supported references remain in the processed-reference recall denominator. '
                'The organizer-label whole-task binary summary is unchanged. Excluding OTHER from class scoring '
                'is a scope correction, not an improvement in recognition or whole-task coverage.')}


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


def _run_evaluation():
    parser = argparse.ArgumentParser(epilog=(
        'A per-user endpoint lock excludes other evaluation CLIs. '
        'It does not reserve the model against service API requests or direct model calls.'))
    parser.add_argument('--manifest', type=Path, default=Path('.local_ml/dataset.json'))
    parser.add_argument('--mode', choices=['benchmark', 'dev', 'holdout', 'video-holdout', 'diagnostic-all'], default='benchmark')
    parser.add_argument('--order', choices=['duration', 'sha256'], default='duration',
                        help='Execution order; diagnostic-all includes dev and old holdout, never quarantine')
    parser.add_argument('--output', type=Path, default=Path('.local_ml/evaluation'))
    parser.add_argument('--limit', type=int)
    parser.add_argument('--budget-sec', type=float, default=210)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8091')
    parser.add_argument('--frames', type=int, default=12)
    parser.add_argument('--sample-fps', type=float, choices=(1.0, 2.0, 4.0), default=2.0,
                        help='Research decode sampling rate; frame budget per model window is unchanged')
    parser.add_argument('--window-sec', type=float, default=24)
    parser.add_argument('--stride-sec', type=float, default=18)
    parser.add_argument('--annotations', type=Path)
    parser.add_argument('--max-side', type=int, default=848)
    parser.add_argument('--input-mode', choices=['images', 'video'], default='images')
    parser.add_argument('--allow-reclassification', action='store_true',
                        help='Research only: reconsider a candidate class with an extra same-episode confirmation')
    parser.add_argument('--complementary-sampling', action='store_true',
                        help='Research only: revisit empty local windows using previously omitted frames')
    parser.add_argument('--motion-sampling', action='store_true',
                        help='Research only: keep uniform anchors and prioritize localized visual changes in local windows')
    parser.add_argument('--strict-episode-matching', action='store_true',
                        help='Research only: require refinement triggers to remain within screening episode ranges')
    model_variant = parser.add_mutually_exclusive_group()
    model_variant.add_argument('--observation-model', action='store_true')
    model_variant.add_argument('--rulewise-model', action='store_true',
                               help='Research only: screen each requested procedure with its own scoped prompt')
    parser.add_argument('--rules', type=Path, default=Path('app/data/ml-rules.json'))
    parser.add_argument('--ids', nargs='+', help='Explicit subset for diagnostics; recorded in report')
    parser.add_argument('--model-lock', type=Path, default=Path('ml_tools/model35-lock.json'),
                        help='Declared runtime weights; does not verify the running server identity')
    args = parser.parse_args()
    if args.observation_model and args.input_mode != 'video':
        parser.error('Observation model requires --input-mode video')
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit must be positive')
    try:
        cfg = AnalyzerConfig(endpoint=args.endpoint, budget_sec=args.budget_sec, frames_per_window=args.frames,
                             sample_fps=args.sample_fps,
                             window_sec=args.window_sec, stride_sec=args.stride_sec, max_side=args.max_side,
                             input_mode=args.input_mode, allow_reclassification=args.allow_reclassification,
                             complementary_sampling=args.complementary_sampling)
    except ValueError as error:
        parser.error(str(error))
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    annotations = json.loads(args.annotations.read_text(encoding='utf-8'))['annotations'] if args.annotations else {}
    if args.mode == 'holdout' and not annotations:
        raise SystemExit('Holdout requires reviewed annotations to revalidate source groups.')
    if annotations:
        validate_annotations(manifest, annotations, require_all=args.mode == 'holdout')
        source_annotations = resolve_annotations_by_sha(manifest['records'], annotations)
        annotations = {record['id']: source_annotations[record['sha256']]
                       for record in manifest['records'] if record['sha256'] in source_annotations}
    if args.mode == 'holdout' and not manifest.get('source_grouping_reviewed'):
        raise SystemExit('Finalize source/camera/session grouping locally before evaluating holdout.')
    split = 'dev' if args.mode == 'benchmark' else 'holdout' if args.mode == 'video-holdout' else args.mode
    eligible_splits = {'dev', 'holdout'} if args.mode == 'diagnostic-all' else {split}
    selected = sorted(select_unique_sources(manifest['records'], eligible_splits, args.ids),
                      key=lambda r: r['duration_sec'])
    if not selected:
        raise SystemExit('Selected split has no eligible videos.')
    if args.mode == 'benchmark' and len(selected) > 1:
        selected = [selected[0], selected[-1]]
    selected.sort(key=lambda r: r['sha256'] if args.order == 'sha256' else r['duration_sec'])
    if args.limit:
        selected = selected[:args.limit]
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit('Choose a fresh --output directory to preserve previous measurements.')
    analyzer_options = {}
    matching_policy = 'interval-overlap'
    if args.strict_episode_matching:
        from ml_tools.episode_matching import MATCHING_POLICY, same_episode
        analyzer_options['candidate_matcher'] = same_episode
        matching_policy = MATCHING_POLICY
    sampling_policy = 'uniform'
    if args.motion_sampling:
        from ml_tools.motion_sampling import SAMPLING_POLICY, select_motion_frames
        analyzer_options['local_frame_selector'] = select_motion_frames
        sampling_policy = SAMPLING_POLICY
    if args.observation_model:
        from ml_tools.observation import ObservationModel, OBSERVATION_PROMPT
        analyzer = QwenLocalAnalyzer(cfg, model=ObservationModel(args.endpoint), **analyzer_options)
        effective_prompt = OBSERVATION_PROMPT
    elif args.rulewise_model:
        from ml_tools.rulewise import RulewiseModel, scoped_prompts
        analyzer = QwenLocalAnalyzer(cfg, model=RulewiseModel(args.endpoint, input_mode=args.input_mode), **analyzer_options)
        effective_prompt = None
    else:
        analyzer = QwenLocalAnalyzer(cfg, **analyzer_options)
        effective_prompt = PROMPT
    catalog = load_rule_catalog(args.rules)
    # Research measures candidate procedures without activating them in the API.
    # Predictions stay private and are not materialized as verified legal findings.
    research_rule_ids = tuple(rule.id for rule in catalog.rules)
    if not research_rule_ids or set(research_rule_ids) - set(RULES):
        raise SystemExit('Research catalog must contain supported candidate procedures')
    component_names = [
        'app/local_analyzer.py', 'app/ml/model.py', 'app/ml/rules.py', 'app/ml/video.py',
        'app/ml/media_input.py', 'ml_tools/observation.py',
    ]
    if args.rulewise_model:
        actual_scoped_prompts = scoped_prompts(research_rule_ids)
        effective_prompt = json.dumps(actual_scoped_prompts, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        component_names.append('ml_tools/rulewise.py')
    if args.motion_sampling:
        component_names.append('ml_tools/motion_sampling.py')
    if args.strict_episode_matching:
        component_names.extend(['ml_tools/episode_matching.py', 'app/ml/episode_matching.py'])
    provenance = {
        'model_lock': json.loads(args.model_lock.read_text(encoding='utf-8')),
        'model_identity_verified': False,
        'prompt_sha256': hashlib.sha256(effective_prompt.encode('utf-8')).hexdigest(),
        'observation_model': args.observation_model,
        'rulewise_model': args.rulewise_model,
        'motion_sampling': args.motion_sampling,
        'sampling_policy': sampling_policy,
        'strict_episode_matching': args.strict_episode_matching,
        'episode_matching_policy': matching_policy,
        'catalog_sha256': hashlib.sha256(args.rules.read_bytes()).hexdigest(),
        'analyzer_sha256': hashlib.sha256(Path(analyzer_module.__file__).read_bytes()).hexdigest(),
        'component_sha256': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in component_names},
    }
    if args.rulewise_model:
        provenance.update(
            prompt_format='canonical_rulewise_prompt_map',
            rulewise_prompts=actual_scoped_prompts,
            rulewise_prompt_sha256={rule_id: hashlib.sha256(prompt.encode('utf-8')).hexdigest()
                                   for rule_id, prompt in actual_scoped_prompts.items()},
        )
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    event_totals = dict(tp=0, fp=0, fn=0, reviewed_videos=0)
    memory = MemorySampler()
    memory.thread.start()
    try:
        for item in selected:
            started = time.monotonic()
            row = {k: item[k] for k in ('id', 'label', 'duration_sec', 'sha256')}
            row['point_sec'] = item.get('point_sec')
            try:
                try:
                    with Path(item['path']).open('rb') as source:
                        source_digest = hashlib.file_digest(source, 'sha256').hexdigest()
                except OSError:
                    # OS exceptions can contain private source paths. Treat an
                    # unavailable source as a technical failure without inference.
                    raise VideoAnalysisError('Local source could not be read for integrity verification') from None
                if source_digest != item['sha256']:
                    raise VideoAnalysisError('Video changed after dataset audit')
                result, metrics = analyzer.analyze_with_metrics(AnalysisContext(job_id=item['id'], video_path=Path(item['path']),
                    duration_sec=item['duration_sec'], enabled_rule_ids=research_rule_ids))
                row.update(status='done', verdict=result.verdict, event_times=[e.timecode_sec for e in result.violations], **metrics)
                # Full observations stay in a private file and are never printed.
                (args.output / f"{item['id']}.result.json").write_text(result.model_dump_json(indent=2), encoding='utf-8')
                annotation = annotations.get(item['id'], {})
                predictions = [{'rule_id': e.rule_id, 'timecode_sec': e.timecode_sec} for e in result.violations]
                matches = score_supported_events(annotation, predictions, research_rule_ids)
                if matches is not None:
                    for key, value in matches.items():
                        event_totals[key] += value
                    event_totals['reviewed_videos'] += 1
            except (VideoAnalysisError, InsufficientEvidenceError) as error:
                row.update(status='failed', error=str(error), error_code=error.code,
                           elapsed_sec=round(time.monotonic() - started, 3))
            rows.append(row)
            report = {'mode': args.mode, 'order': args.order, 'configuration': asdict(cfg), 'summary': summarize(rows),
                      'provenance': provenance,
                      'rule_ids': list(research_rule_ids), 'catalog_activation': 'research_only', 'explicit_ids': args.ids,
                      'evaluation_scope': ('Whole-video split with exact-duplicate grouping only; camera/session membership is unverified. Not an unseen-view evaluation.'
                          if args.mode == 'video-holdout' else 'Combined development and previously used holdout; not a held-out test.'
                          if args.mode == 'diagnostic-all' else 'Development/diagnostic unless grouped holdout explicitly requested.'),
                      'event_metrics_completed_only': event_totals if annotations else None,
                      'reference_scope': audit_reference_scope(selected, annotations, research_rule_ids, rows,
                                                               event_totals),
                      'gpu_peak_used_mib': memory.peak, 'rows': rows}
            (args.output / 'metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'progress': len(rows), 'total': len(selected),
                              'status': row['status'], 'elapsed_sec': row['elapsed_sec']}), flush=True)
        print(json.dumps({'summary': summarize(rows), 'gpu_peak_used_mib': memory.peak}, ensure_ascii=False), flush=True)
    finally:
        memory.stop.set()
        memory.thread.join(timeout=4)


def main():
    # Parse only the lock identity here; the existing parser validates all other
    # arguments inside the lock, before creating an evaluation output directory.
    if '-h' in sys.argv[1:] or '--help' in sys.argv[1:]:
        _run_evaluation()
        return
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8091')
    args, _ = parser.parse_known_args()
    try:
        lock = EvaluationLock(args.endpoint)
    except ValueError:
        parser.error('Model endpoint must be literal loopback HTTP with an explicit port.')
    try:
        with lock:
            _run_evaluation()
    except EvaluationLockError as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
