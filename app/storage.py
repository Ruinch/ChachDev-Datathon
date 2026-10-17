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
            summary_columns = {
                "verdict": "TEXT",
                "event_count": "INTEGER NOT NULL DEFAULT 0",
                "analysis_scope_json": "TEXT",
            }
            needs_summary_migration = bool(summary_columns.keys() - columns)
            for name, definition in summary_columns.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            if needs_summary_migration:
                # Extract only small fields in SQLite once. History queries must never
                # load stored frames or descriptions into the API process.
                conn.execute("""
                    UPDATE jobs SET
                        verdict = CASE WHEN status = 'done' AND json_valid(result_json)
                            THEN CASE WHEN json_extract(result_json, '$.verdict') IN ('violation', 'no_violation')
                                THEN json_extract(result_json, '$.verdict') END END,
                        event_count = CASE WHEN status = 'done' AND json_valid(result_json)
                            THEN CASE WHEN json_type(result_json, '$.violations') = 'array'
                                THEN json_array_length(result_json, '$.violations') ELSE 0 END ELSE 0 END,
                        analysis_scope_json = CASE WHEN status = 'done' AND json_valid(result_json)
                            THEN CASE WHEN json_type(result_json, '$.analysis_scope') = 'object'
                                THEN json_extract(result_json, '$.analysis_scope') END END
                """)
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_history_order ON jobs(created_at DESC, job_id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_verdict ON jobs(verdict)")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.create_function("unicode_casefold", 1, lambda value: value.casefold() if isinstance(value, str) else "", deterministic=True)
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

    def get_metadata(self, job_id: str) -> dict | None:
        """Read source information without fetching or decoding evidence payloads."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT filename, created_at, updated_at, metadata_json FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            **_json_object(row["metadata_json"]),
            "filename": row["filename"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def set_done(self, job_id: str, result: dict) -> None:
        verdict = result.get("verdict")
        if verdict not in ("violation", "no_violation"):
            verdict = None
        events = result.get("violations")
        scope = result.get("analysis_scope")
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE jobs SET status = 'done', updated_at = ?, result_json = ?, "
                "verdict = ?, event_count = ?, analysis_scope_json = ?, "
                "error_code = NULL, error_message = NULL "
                "WHERE job_id = ? AND status = 'processing'",
                (utc_now(), json.dumps(result, ensure_ascii=False), verdict,
                 len(events) if isinstance(events, list) else 0,
                 json.dumps(scope, ensure_ascii=False) if isinstance(scope, dict) else None, job_id),
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

    def list_jobs(
        self, *, limit: int = 20, offset: int = 0, q: str = "", status: str = "all", verdict: str = "all",
    ) -> dict:
        conditions: list[str] = []
        parameters: list[str | int] = []
        if q:
            # LIKE's wildcards are literal filename characters supplied by the user.
            escaped = q.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("unicode_casefold(filename) LIKE ? ESCAPE '\\'")
            parameters.append(f"%{escaped}%")
        if status != "all":
            conditions.append("status = ?")
            parameters.append(status)
        if verdict != "all":
            conditions.append("status = 'done' AND verdict = ?")
            parameters.append(verdict)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connection() as conn:
            # Counts and page use a consistent snapshot while an analysis completes.
            conn.execute("BEGIN")
            summary = dict(conn.execute("""
                SELECT COUNT(*) AS total,
                    COALESCE(SUM(status = 'processing'), 0) AS processing,
                    COALESCE(SUM(status = 'done'), 0) AS done,
                    COALESCE(SUM(status = 'failed'), 0) AS failed,
                    COALESCE(SUM(status = 'done' AND verdict = 'violation'), 0) AS violation,
                    COALESCE(SUM(status = 'done' AND verdict = 'no_violation'), 0) AS no_violation
                FROM jobs
            """).fetchone())
            total = conn.execute("SELECT COUNT(*) FROM jobs" + where, parameters).fetchone()[0]
            rows = conn.execute(
                "SELECT job_id, status, filename, created_at, updated_at, metadata_json, "
                "verdict, event_count, analysis_scope_json FROM jobs" + where
                + " ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
        items = []
        for row in rows:
            raw_metadata = _json_object(row["metadata_json"])
            item = {
                "job_id": row["job_id"], "status": row["status"], "verdict": row["verdict"],
                "event_count": row["event_count"],
                "metadata": {
                    "filename": row["filename"],
                    **{key: raw_metadata.get(key) for key in ("size_bytes", "duration_sec", "width", "height", "codec")},
                    "created_at": row["created_at"], "updated_at": row["updated_at"],
                },
            }
            scope = _json_object(row["analysis_scope_json"])
            if scope:
                item["analysis_scope"] = scope
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset, "summary": summary}

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def evidence_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "evidence"


def _json_object(value: str | None) -> dict:
    try:
        result = json.loads(value) if value else {}
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}
