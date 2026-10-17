"""Analyze one local video and write private JSON without printing observations."""
import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from app.analyzer import AnalysisContext, InsufficientEvidenceError
from app.evidence import materialize_result
from app.local_analyzer import AnalyzerConfig, QwenLocalAnalyzer
from app.ml.video import VideoAnalysisError
from app.rules import load_rule_catalog
from app.video import VideoValidationError, probe_video


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('video', type=Path)
    parser.add_argument('--output', type=Path, default=Path('.local_ml/single-result.json'))
    parser.add_argument('--endpoint', default='http://127.0.0.1:8091')
    parser.add_argument('--rules', type=Path, default=Path('app/data/ml-rules.json'))
    parser.add_argument('--ffprobe', default=os.getenv('APP_FFPROBE_BIN', '.local_tools/ffmpeg/bin/ffprobe.exe'))
    parser.add_argument('--evidence', action='store_true', help='Extract real JPEGs and write complete API JSON')
    args = parser.parse_args()
    job_id = str(uuid4())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit('Choose a fresh --output file to preserve earlier results')
    try:
        duration = probe_video(args.video, args.ffprobe, 15, 180).duration_sec
        catalog = load_rule_catalog(args.rules)
        rule_ids = catalog.enabled_rule_ids if args.evidence else tuple(rule.id for rule in catalog.rules)
        result, metrics = QwenLocalAnalyzer(AnalyzerConfig(endpoint=args.endpoint)).analyze_with_metrics(
            AnalysisContext(job_id, args.video, duration, rule_ids))
        if args.evidence:
            import imageio_ffmpeg
            document = materialize_result(job_id, args.video, args.output.parent / (job_id + '-evidence'), result,
                os.getenv('APP_FFMPEG_BIN', imageio_ffmpeg.get_ffmpeg_exe()), duration, catalog).model_dump(mode='json')
        else:
            document = {'job_id': job_id, 'status': 'done', 'mode': 'research_candidates', **result.model_dump(mode='json')}
        print(json.dumps({'status': 'done', **metrics}))
    except (VideoAnalysisError, InsufficientEvidenceError, VideoValidationError) as error:
        document = {'job_id': job_id, 'status': 'failed', 'error': {'code': error.code, 'message': str(error)}}
        print(json.dumps({'status': 'failed', 'error': str(error)}))
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    if document['status'] == 'failed':
        sys.exit(1)


if __name__ == '__main__':
    main()
