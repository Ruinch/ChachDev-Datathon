from __future__ import annotations

import functools
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from ml_tools import evaluate
from ml_tools.evaluation_lock import (
    EvaluationAlreadyRunning, EvaluationLock, EvaluationLockError, normalize_endpoint,
)


_CHILD = '''
import sys
from pathlib import Path
from ml_tools.evaluation_lock import EvaluationLock, EvaluationAlreadyRunning
try:
    with EvaluationLock(sys.argv[1], directory=Path(sys.argv[2])):
        if sys.argv[3] != '-':
            Path(sys.argv[3]).write_text('locked', encoding='ascii')
            sys.stdin.readline()
        print('acquired', flush=True)
except EvaluationAlreadyRunning as error:
    print(str(error), flush=True)
    raise SystemExit(23)
'''
_ROOT = Path(__file__).resolve().parents[1]


def child_command(endpoint, directory, ready='-'):
    return [sys.executable, '-c', _CHILD, endpoint, str(directory), str(ready)]


@contextmanager
def holder(tmp_path, endpoint='http://127.0.0.1:8091'):
    directory = tmp_path / 'locks'
    ready = tmp_path / 'ready'
    process = subprocess.Popen(child_command(endpoint, directory, ready), cwd=_ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail('Synthetic lock holder exited before acquiring its lock: ' + process.stderr.read())
            if time.monotonic() > deadline:
                pytest.fail('Synthetic lock holder did not acquire its lock within ten seconds')
            time.sleep(0.01)
        yield process, directory
    finally:
        if process.poll() is None:
            try:
                process.stdin.write('release\n')
                process.stdin.flush()
                process.wait(timeout=5)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                process.terminate()
                process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


def attempt(endpoint, directory):
    return subprocess.run(child_command(endpoint, directory), cwd=_ROOT, capture_output=True,
                          text=True, timeout=8,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def test_second_process_fails_immediately_for_normalized_same_endpoint_then_can_acquire(tmp_path):
    with holder(tmp_path) as (process, directory):
        result = attempt('http://127.0.0.1:08091/v1/', directory)
        assert result.returncode == 23
        assert result.stdout.strip() == 'Local model evaluation already running for this endpoint.'
        assert not result.stderr
        assert process.poll() is None  # Contender did not wait for owner to finish.
    assert attempt('http://127.0.0.1:8091', directory).returncode == 0


def test_different_endpoint_is_allowed_while_first_process_holds_lock(tmp_path):
    with holder(tmp_path) as (process, directory):
        result = attempt('http://127.0.0.1:8092', directory)
        assert result.returncode == 0 and result.stdout.strip() == 'acquired'
        assert process.poll() is None


def test_process_death_releases_lock_without_deleting_lock_file(tmp_path):
    with holder(tmp_path) as (process, directory):
        path = EvaluationLock('http://127.0.0.1:8091', directory=directory).path
        assert path.is_file()
        process.terminate()  # Only this test's synthetic helper, never a service.
        process.wait(timeout=5)
        assert path.is_file()
        assert attempt('http://127.0.0.1:8091', directory).returncode == 0


def test_exception_releases_ownership_and_normalizes_ipv6(tmp_path):
    assert normalize_endpoint('http://[::1]:08091/v1/') == 'http://[::1]:8091'
    with pytest.raises(RuntimeError, match='synthetic failure'):
        with EvaluationLock('http://[::1]:8091', directory=tmp_path):
            raise RuntimeError('synthetic failure')
    with EvaluationLock('http://[::1]:08091/v1/', directory=tmp_path):
        pass


def test_default_lock_location_is_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local-app-data'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    before = EvaluationLock('http://127.0.0.1:8091').path
    monkeypatch.chdir(tmp_path)
    assert EvaluationLock('http://127.0.0.1:08091/v1/').path == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows Store LOCALAPPDATA redirection')
def test_windows_default_lock_uses_documents_independent_of_localappdata(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'host-local-app-data'))
    before = EvaluationLock('http://127.0.0.1:8091').path
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'store-virtualized-local-app-data'))
    assert EvaluationLock('http://127.0.0.1:8091/v1/').path == before
    assert before.parent == tmp_path / 'Documents' / 'ForteBankDatathon' / 'evaluation-locks'
    assert not before.exists()  # Resolving a lock path does not acquire/create one.


@pytest.mark.parametrize('endpoint', [
    'https://127.0.0.1:8091', 'http://example.invalid:8091', 'http://127.0.0.1',
    'http://user:secret@127.0.0.1:8091', 'http://127.0.0.1:8091/?key=secret',
])
def test_invalid_endpoint_is_rejected_before_creating_lock_files(tmp_path, endpoint):
    directory = tmp_path / 'never-created'
    with pytest.raises(ValueError):
        EvaluationLock(endpoint, directory=directory)
    assert not directory.exists()


def test_lock_creation_errors_do_not_echo_filesystem_paths(tmp_path):
    directory = tmp_path / 'private-path'
    directory.write_text('not a directory')
    with pytest.raises(EvaluationLockError) as caught:
        with EvaluationLock('http://127.0.0.1:8091', directory=directory):
            pytest.fail('Cannot acquire a lock under a regular file')
    assert str(caught.value) == 'Cannot create the local model evaluation lock.'


def test_cli_holds_lock_over_whole_invocation_and_releases_on_error(tmp_path, monkeypatch):
    endpoint = 'http://127.0.0.1:8091'
    monkeypatch.setattr(evaluate, 'EvaluationLock', functools.partial(EvaluationLock, directory=tmp_path))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--endpoint', endpoint])

    def fail_inside_evaluation():
        with pytest.raises(EvaluationAlreadyRunning):
            with EvaluationLock(endpoint, directory=tmp_path):
                pytest.fail('Evaluation must own the endpoint lock before its work begins')
        raise RuntimeError('synthetic evaluation failure')

    monkeypatch.setattr(evaluate, '_run_evaluation', fail_inside_evaluation)
    with pytest.raises(RuntimeError, match='synthetic evaluation failure'):
        evaluate.main()
    with EvaluationLock(endpoint, directory=tmp_path):
        pass


def test_busy_cli_does_not_begin_evaluation(tmp_path, monkeypatch):
    endpoint = 'http://127.0.0.1:8091'
    monkeypatch.setattr(evaluate, 'EvaluationLock', functools.partial(EvaluationLock, directory=tmp_path))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--endpoint', endpoint])
    monkeypatch.setattr(evaluate, '_run_evaluation', lambda: pytest.fail('Busy CLI must not read data or start work'))
    with EvaluationLock(endpoint, directory=tmp_path):
        with pytest.raises(SystemExit, match='Local model evaluation already running'):
            evaluate.main()


def test_cli_help_remains_available_without_acquiring_lock(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--help'])
    monkeypatch.setattr(evaluate, 'EvaluationLock', lambda *args, **kwargs: pytest.fail('Help does not analyze'))
    with pytest.raises(SystemExit) as caught:
        evaluate.main()
    assert caught.value.code == 0
    assert 'does not reserve the model against service API requests' in ' '.join(capsys.readouterr().out.split())
