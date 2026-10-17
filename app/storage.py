from __future__ import annotations

import json
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
                    error_code TEXT,
                    error_message TEXT
                )
                """
            )

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

    def create(self, job_id: str, filename: str) -> str:
        created_at = utc_now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, status, filename, created_at, updated_at) "
                "VALUES (?, 'processing', ?, ?, ?)",
                (job_id, filename, created_at, created_at),
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
