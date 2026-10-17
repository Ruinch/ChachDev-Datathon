"""Explicit one-time download of pinned public weights/runtime; no dataset input."""
import argparse
import concurrent.futures
import hashlib
import json
import shutil
import urllib.request
import zipfile
import zlib
from pathlib import Path


def install(lock: Path, destination: Path):
    manifest = json.loads(lock.read_text(encoding='utf-8-sig'))
    destination.mkdir(parents=True, exist_ok=True)

    def download(item):
        folder = destination / ('downloads' if item['kind'] == 'runtime' else 'models')
        folder.mkdir(exist_ok=True)
        if Path(item['name']).name != item['name']:
            raise ValueError('Lock file contains an invalid filename')
        target = folder / item['name']
        if not target.exists():
            partial = target.with_suffix(target.suffix + '.part')
            with urllib.request.urlopen(item['url'], timeout=90) as response, partial.open('wb') as output:
                shutil.copyfileobj(response, output, 1024*1024)
            with partial.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest != item['sha256']:
                raise ValueError('Downloaded checksum mismatch: ' + item['name'])
            partial.replace(target)
        with target.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != item['sha256']:
                raise ValueError('Checksum mismatch: ' + item['name'])
        print('Verified: ' + item['name'], flush=True)
        return target, item

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        files = list(pool.map(download, manifest['files']))
    for path, item in files:
        if item['kind'] == 'runtime':
            folder = destination / 'llama'
            folder.mkdir(exist_ok=True)
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if not member.is_dir():
                        target = folder / Path(member.filename).name
                        if target.exists() and target.stat().st_size == member.file_size:
                            crc = 0
                            with target.open('rb') as existing:
                                for chunk in iter(lambda: existing.read(1024*1024), b''):
                                    crc = zlib.crc32(chunk, crc)
                            if crc == member.CRC:
                                continue
                        with archive.open(member) as source, target.open('wb') as output:
                            shutil.copyfileobj(source, output)
    (destination / ('installed-' + lock.name)).write_text(json.dumps(manifest, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--lock', type=Path, default=Path('ml_tools/model-lock.json'))
    parser.add_argument('--destination', type=Path, default=Path('.local_ml'))
    args = parser.parse_args()
    install(args.lock, args.destination)
