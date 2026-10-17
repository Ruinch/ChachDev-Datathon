from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JobStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.jobs_dir = self.data_dir / "jobs"
        self.db_path = self.data_dir / "jobs.sqlite3"
        self._server_lock = None

    def acquire_server_lock(self) -> None:
        """The data directory and local GPU belong to one API process at a time."""
        lock = (self.data_dir / ".server.lock").open("a+b")
        try:
            if lock.seek(0, 2) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise RuntimeError("This APP_DATA_DIR is already used by another API process; run one Uvicorn worker") from exc
        self._server_lock = lock

    def release_server_lock(self) -> None:
        if self._server_lock is not None:
            self._server_lock.close()
            self._server_lock = None

    def initialize(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    metadata_json TEXT,
                    error_code TEXT,
                    error_message TEXT
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "metadata_json" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN metadata_json TEXT")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, job_id: str, filename: str, metadata: dict | None = None) -> str:
        created_at = utc_now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, status, filename, created_at, updated_at, metadata_json) "
                "VALUES (?, 'processing', ?, ?, ?, ?)",
                (job_id, filename, created_at, created_at, json.dumps(metadata or {})),
            )
        return created_at

    def get(self, job_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        raw_result = record.pop("result_json")
        record["result"] = json.loads(raw_result) if raw_result else None
        raw_metadata = record.pop("metadata_json", None)
        record["metadata"] = {
            **(json.loads(raw_metadata) if raw_metadata else {}),
            "filename": record["filename"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }
        return record

    def set_done(self, job_id: str, result: dict) -> None:
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE jobs SET status = 'done', updated_at = ?, result_json = ?, "
                "error_code = NULL, error_message = NULL "
                "WHERE job_id = ? AND status = 'processing'",
                (utc_now(), json.dumps(result, ensure_ascii=False), job_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError("Job is no longer processing")

    def set_failed(self, job_id: str, code: str, message: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', updated_at = ?, result_json = NULL, "
                "error_code = ?, error_message = ? "
                "WHERE job_id = ? AND status = 'processing'",
                (utc_now(), code, message, job_id),
            )

    def fail_interrupted(self) -> None:
        """A single-process server cannot resume work lost during a restart."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', updated_at = ?, "
                "error_code = 'interrupted', "
                "error_message = 'Processing was interrupted by a server restart' "
                "WHERE status = 'processing'",
                (utc_now(),),
            )

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def evidence_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "evidence"
