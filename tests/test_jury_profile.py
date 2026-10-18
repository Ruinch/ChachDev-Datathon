"""The preparation command must isolate jury data without publishing or activating ML."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_prepare_jury_creates_private_clean_profile_and_never_overwrites(tmp_path):
    root = tmp_path/'jury'
    command = [sys.executable, '-m', 'ml_tools.prepare_jury', '--destination', str(root)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    env = dict(line.split('=', 1) for line in (root/'service.env').read_text(encoding='utf-8').splitlines()
               if line and not line.startswith('#'))
    key = env['APP_API_KEY']
    assert len(key) >= 64 and key not in first.stdout + first.stderr
    assert Path(env['APP_DATA_DIR']) == root/'data'
    assert list((root/'data').iterdir()) == []
    assert env['APP_ML_INPUT_MODE'] == 'video' and env['APP_ML_FRAMES'] == '24'
    rules = json.loads(Path(env['APP_RULE_CATALOG_PATH']).read_text(encoding='utf-8'))
    assert not any(r['enabled'] for r in rules['rules'])
    (root/'data'/'keep.txt').write_text('existing jury data')
    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode != 0
    assert key in (root/'service.env').read_text(encoding='utf-8')
    assert (root/'data'/'keep.txt').read_text() == 'existing jury data'
    assert key not in second.stdout + second.stderr


@pytest.mark.skipif(os.name != 'nt', reason='Windows launcher')
def test_jury_launcher_ignores_personal_environment_and_keeps_quality_gate(tmp_path):
    root = tmp_path/'jury'
    subprocess.run([sys.executable, '-m', 'ml_tools.prepare_jury', '--destination', str(root)],
                   check=True, capture_output=True)
    env = os.environ.copy()
    env.update(APP_DATA_DIR=str(tmp_path/'personal'), APP_API_KEY='personal-test-secret',
               APP_RULE_CATALOG_PATH='missing-personal-catalog.json', APP_LOCAL_MODEL_URL='http://example.org:9999')
    command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
               'scripts/start-jury.ps1', '-ProfilePath', str(root), '-CheckOnly']
    inspection = subprocess.run(command+['-InterfaceOnly'], env=env, capture_output=True, text=True)
    assert inspection.returncode == 0, inspection.stderr
    deployment = subprocess.run(command, env=env, capture_output=True, text=True)
    assert deployment.returncode != 0
    assert 'no enabled verified rules' in deployment.stderr
    normal_start = subprocess.run(command[:-1], env=env, capture_output=True, text=True, timeout=30)
    assert normal_start.returncode != 0
    assert 'no enabled verified rules' in normal_start.stderr
    assert not (tmp_path/'personal').exists()
    assert 'personal-test-secret' not in inspection.stdout+inspection.stderr+deployment.stdout+deployment.stderr


@pytest.mark.skipif(os.name != 'nt', reason='Windows launcher')
@pytest.mark.skipif(
    not Path('.local_ml/llama/llama-server.exe').is_file(),
    reason='Requires the prepared local runtime; downloads are forbidden in this check',
)
def test_jury_can_check_model_files_with_disabled_rules_without_starting_servers(tmp_path):
    root = tmp_path/'jury'
    subprocess.run([sys.executable, '-m', 'ml_tools.prepare_jury', '--destination', str(root)],
                   check=True, capture_output=True)
    initial_rules = (root/'rules.json').read_bytes()
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         'scripts/start-jury.ps1', '-ProfilePath', str(root),
         '-CheckOnly', '-InterfaceOnly', '-StartModel'],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert 'Local model files verified.' in result.stdout
    assert 'without starting a server' in result.stdout
    assert (root/'rules.json').read_bytes() == initial_rules
    assert list((root/'data').iterdir()) == []
    strict = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         'scripts/start-jury.ps1', '-ProfilePath', str(root), '-CheckOnly', '-StartModel'],
        capture_output=True, text=True, timeout=30,
    )
    assert strict.returncode != 0
    assert 'no enabled verified rules' in strict.stderr


@pytest.mark.skipif(os.name != 'nt', reason='Windows launcher')
def test_laptop_wrapper_retains_startup_stderr_in_private_logs_without_printing_key(tmp_path):
    root = tmp_path/'jury'
    subprocess.run([sys.executable, '-m', 'ml_tools.prepare_jury', '--destination', str(root)],
                   check=True, capture_output=True)
    key = next(line.split('=', 1)[1] for line in (root/'service.env').read_text().splitlines()
               if line.startswith('APP_API_KEY='))
    manifest = json.loads((root/'profile.json').read_text())
    manifest['purpose'] = 'invalid-test-profile'
    (root/'profile.json').write_text(json.dumps(manifest))
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         'scripts/run-laptop.ps1', '-ProfilePath', str(root)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    runs = list((root/'logs').glob('startup-*'))
    assert runs, result.stdout + result.stderr
    run = runs[0]
    stderr = (run/'stderr.log').read_bytes()
    assert b'Invalid jury profile' in stderr
    assert (run/'transcript.log').is_file()
    all_output = (result.stdout + result.stderr).encode()
    all_output += b''.join(path.read_bytes() for path in run.iterdir())
    assert key.encode() not in all_output
    assert list((root/'data').iterdir()) == []
