"""Exercise the real local worker/API on one authorized local video; output stays private."""
from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
from pathlib import Path
import secrets
import time

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--rules', type=Path, default=Path('app/data/ml-rules.json'))
    parser.add_argument('--ffmpeg', default='.local_tools/ffmpeg/bin/ffmpeg.exe')
    parser.add_argument('--ffprobe', default='.local_tools/ffmpeg/bin/ffprobe.exe')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a new output directory')
    args.output.mkdir(parents=True)
    key = secrets.token_hex(32)
    settings = Settings(api_key=key, data_dir=args.output/'jobs', ffmpeg_bin=str(Path(args.ffmpeg).resolve()),
        ffprobe_bin=str(Path(args.ffprobe).resolve()), analyzer_factory='app.local_analyzer:build_analyzer',
        rule_catalog_path=args.rules.resolve())
    headers = {'X-API-Key': key}
    started = time.monotonic()
    with TestClient(create_app(settings)) as client:
        if not client.get('/api/v1/system', headers=headers).json()['ready']:
            raise SystemExit('Local API is not ready')
        with args.video.open('rb') as stream:
            response = client.post('/api/v1/analyze', headers=headers, files={'file': ('input.mov', stream, 'video/quicktime')})
        if response.status_code != 202:
            raise SystemExit(f'Upload failed: HTTP {response.status_code}')
        job_id = response.json()['job_id']
        while time.monotonic() - started < 295:
            result = client.get('/api/v1/jobs/'+job_id, headers=headers).json()
            if result['status'] != 'processing':
                break
            time.sleep(0.5)
        (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        frames = 0
        for event in result.get('violations', []):
            for frame in event['evidence_frames']:
                with Image.open(BytesIO(base64.b64decode(frame['image_base64'], validate=True))) as image:
                    if image.format != 'JPEG':
                        raise SystemExit('Evidence is not JPEG')
                    image.load()
                frames += 1
        summary = {'status': result['status'], 'verdict': result.get('verdict'), 'events': len(result.get('violations', [])),
            'valid_jpeg_frames': frames, 'elapsed_sec': round(time.monotonic()-started, 3),
            'error_code': result.get('error', {}).get('code')}
        (args.output/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(summary))
        if result['status'] != 'done':
            raise SystemExit(1)


if __name__ == '__main__':
    main()
