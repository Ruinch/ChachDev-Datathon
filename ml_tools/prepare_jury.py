"""Create a fresh private jury profile. Never starts servers or enables rules."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import secrets
import subprocess


def prepare(destination: Path) -> Path:
    project = Path(__file__).resolve().parents[1]
    destination = destination.resolve()
    if any(c in str(destination) for c in '\r\n'):
        raise ValueError('Profile path must fit on one line')
    catalog = json.loads((project/'app/data/ml-rules.json').read_text(encoding='utf-8'))
    # A fresh installation cannot inherit acceptance merely from a copied catalog.
    for rule in catalog['rules']:
        rule.update(enabled=False, verification='candidate', verified_on=None)
    destination.mkdir(parents=True, exist_ok=False)
    # Restrict the new directory BEFORE writing any secret. Existing paths are never changed.
    if os.name == 'nt':
        identity = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'])
        sid = next(csv.reader(identity.decode(errors='replace').strip().splitlines()))[1]
        if not sid.startswith('S-1-'):
            raise RuntimeError('Could not determine owner SID')
        subprocess.run(['icacls', str(destination), '/inheritance:r', '/grant:r',
                        f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F',
                        '*S-1-5-32-544:(OI)(CI)F'], check=True, capture_output=True)
    else:
        destination.chmod(0o700)
    for name in ('data', 'logs'):
        (destination/name).mkdir()
    rules_path = destination/'rules.json'
    rules_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    values = {
        'APP_API_KEY': secrets.token_hex(32),
        'APP_DATA_DIR': str(destination/'data'),
        'APP_RULE_CATALOG_PATH': str(rules_path),
        'APP_ANALYZER_FACTORY': 'app.local_analyzer:build_analyzer',
        'APP_LOCAL_MODEL_URL': 'http://127.0.0.1:8091',
        'APP_ML_INPUT_MODE': 'video', 'APP_ML_FRAMES': '24',
        'APP_ML_MAX_SIDE': '848', 'APP_ML_BUDGET_SECONDS': '210',
        'APP_MAX_UPLOAD_MB': '128', 'APP_MAX_VIDEO_SECONDS': '180',
        'APP_UPLOAD_TIMEOUT_SECONDS': '30', 'APP_FFPROBE_TIMEOUT_SECONDS': '15',
        'APP_ANALYSIS_TIMEOUT_SECONDS': '240',
        'APP_FFMPEG_BIN': str(project/'.local_tools/ffmpeg/bin/ffmpeg.exe'),
        'APP_FFPROBE_BIN': str(project/'.local_tools/ffmpeg/bin/ffprobe.exe'),
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
    }
    env_path = destination/'service.env'
    with env_path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('# Private jury profile. Rules remain disabled pending acceptance.\n')
        stream.writelines(f'{name}={value}\n' for name, value in values.items())
    if os.name != 'nt':
        env_path.chmod(0o600)
    (destination/'profile.json').write_text(json.dumps({
        'version': 1, 'purpose': 'jury', 'data_dir': str(destination/'data'),
        'env_path': str(env_path), 'rules_path': str(rules_path),
        'quality_accepted': False,
    }, indent=2), encoding='utf-8')
    return env_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ.get('LOCALAPPDATA', Path.home()/'.local/share'))/'ForteBankDatathon'/'jury'
    parser.add_argument('--destination', type=Path, default=default)
    args = parser.parse_args()
    try:
        path = prepare(args.destination)
    except FileExistsError:
        raise SystemExit('Destination already exists; preserved without changes. Reuse its profile or choose a fresh directory.')
    print('Prepared private profile: '+str(path))
    print('Key saved only in service.env. Data empty. Rules disabled. No public access opened.')


if __name__ == '__main__':
    main()
