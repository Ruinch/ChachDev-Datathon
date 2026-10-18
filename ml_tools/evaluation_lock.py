"""Nonblocking, per-user exclusion for cooperating research evaluation CLIs.

The lock is keyed by normalized loopback endpoint, independently of the checkout.
Windows uses a one-byte OS file lock; POSIX uses flock. The OS releases ownership
on process exit, including termination. Empty lock files deliberately remain so
another process cannot open a different inode while an earlier holder still runs.

This does NOT reserve the GPU against the service API, direct model calls, or old
evaluation processes which were already running without this lock.
"""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from app.ml.model import validate_endpoint


class EvaluationLockError(RuntimeError):
    """A safe error without filesystem paths or request contents."""


class EvaluationAlreadyRunning(EvaluationLockError):
    def __init__(self):
        super().__init__('Local model evaluation already running for this endpoint.')


def normalize_endpoint(endpoint: str) -> str:
    parsed = urlsplit(validate_endpoint(endpoint))
    host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    return f'http://{host}:{parsed.port}'


def _default_directory() -> Path:
    if os.name == 'nt':
        # Windows Store apps can virtualize LOCALAPPDATA differently from the
        # owner's PowerShell. Both must resolve this same physical lock path.
        base = Path.home() / 'Documents'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return base / 'ForteBankDatathon' / 'evaluation-locks'


class EvaluationLock:
    def __init__(self, endpoint: str, *, directory: Path | None = None):
        self.endpoint = normalize_endpoint(endpoint)
        key = hashlib.sha256(self.endpoint.encode('ascii')).hexdigest()
        self.path = (directory if directory is not None else _default_directory()) / f'{key}.lock'
        self._fd: int | None = None

    def __enter__(self):
        if self._fd is not None:
            raise EvaluationLockError('Evaluation lock object is already in use.')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            raise EvaluationLockError('Cannot create the local model evaluation lock.') from None
        try:
            # A range beyond EOF can be locked on Windows; no write or truncate is
            # needed, avoiding races while initializing a shared lock file.
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise EvaluationAlreadyRunning() from None
            raise EvaluationLockError('Cannot acquire the local model evaluation lock.') from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        fd, self._fd = self._fd, None
        if fd is None:
            return False
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            if exc_type is None:
                raise EvaluationLockError('Cannot release the local model evaluation lock.') from None
        finally:
            os.close(fd)
        return False
