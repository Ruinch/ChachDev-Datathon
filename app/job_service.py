"""Own the local job lifecycle behind one interface shared by HTTP callers.

The upload admission owns the single processing slot until a fully saved,
validated job is handed to the existing runner. Filesystem and SQLite details
stay here; neither route handlers nor the model adapter coordinate them.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import AsyncIterator, BinaryIO, Callable
from uuid import UUID, uuid4

from app.analyzer import UnconfiguredAnalyzer
from app.analyzer_readiness import analyzer_runtime_status
from app.config import Settings
from app.rules import load_rule_catalog
from app.review import review_rule_ids
from app.storage import JobStore
from app.video import VideoInfo, VideoValidationError
from app.worker import JobRunner


logger = logging.getLogger(__name__)


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, headers: dict | None = None) -> None:
        super().__init__(message)
        self.status_code, self.code, self.headers = status_code, code, headers

    @property
    def detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class SourceVideo:
    path: Path
    filename: str


class _UploadAdmission:
    """A request-scoped capability: accept once, otherwise return its slot."""

    def __init__(self, service: JobService, *, review: bool = False) -> None:
        self._service = service
        self._review = review
        self._transferred = False
        self._used = False

    async def accept(self, filename: str | None, source: BinaryIO) -> dict:
        if self._used:
            raise RuntimeError("An upload admission can accept only one video")
        self._used = True
        # Multipart receiving has already finished under the HTTP guard's limits.
        # Save, probe and commit together outside the event loop. Keep ownership
        # until the thread finishes even when its HTTP request is cancelled.
        pending = asyncio.create_task(asyncio.to_thread(self._service._persist_upload, filename, source, review=self._review))
        cancellation = None
        while True:
            try:
                accepted = await asyncio.shield(pending)
                break
            except asyncio.CancelledError as exc:
                if pending.cancelled():
                    raise
                # Each later cancel must also leave the persistence task intact.
                # The admission and UploadFile remain owned until this completes.
                cancellation = exc
            except Exception:
                # Persistence cleans up before raising. Preserve the caller's
                # cancellation after cleanup rather than replying on a dead HTTP request.
                if cancellation is not None:
                    raise cancellation
                raise
        self._start(accepted["job_id"])
        if cancellation is not None:
            raise cancellation
        return accepted

    def _start(self, job_id: str) -> None:
        self._transferred = True
        self._service._runner.start(job_id)


class JobService:
    def __init__(
        self, settings: Settings, store: JobStore, runner: JobRunner, *,
        probe: Callable[[Path, str, float, float], VideoInfo],
        tools_available: Callable[[str], bool],
    ) -> None:
        self._settings, self._store, self._runner = settings, store, runner
        self._probe, self._tools_available = probe, tools_available
        self._catalog = None
        self._catalog_error = False

    @asynccontextmanager
    async def lifespan(self):
        self._settings.validate()
        self._store.initialize()
        self._store.acquire_server_lock()
        try:
            self._store.fail_interrupted()
            try:
                self._catalog = load_rule_catalog(self._settings.rule_catalog_path)
                self._catalog_error = False
            except Exception:
                self._catalog = None
                self._catalog_error = True
                logger.exception("The rule catalog could not be loaded")
            self._runner.startup()
            try:
                yield
            finally:
                await asyncio.to_thread(self._runner.shutdown)
        finally:
            self._store.release_server_lock()

    def status(self) -> dict:
        configured = bool(self._settings.analyzer_factory) or (
            self._runner.analyzer is not None and not isinstance(self._runner.analyzer, UnconfiguredAnalyzer)
        )
        enabled = list(self._catalog.enabled_rule_ids) if self._catalog is not None else []
        tools = {name: self._tools_available(binary) for name, binary in (
            ("ffmpeg", self._settings.ffmpeg_bin), ("ffprobe", self._settings.ffprobe_bin),
        )}
        runtime = analyzer_runtime_status(self._settings.analyzer_factory)
        reason = None
        if not configured:
            reason = {"code": "analyzer_unavailable", "message": "A local video analyzer has not been configured"}
        elif self._catalog_error:
            reason = {"code": "invalid_rule_catalog", "message": "The local rule catalog is missing or invalid"}
        elif not enabled:
            reason = {"code": "analysis_not_ready", "message": "No verified rules have been enabled"}
        elif not all(tools.values()):
            reason = {"code": "video_tools_unavailable", "message": "Local FFmpeg and FFprobe are required"}
        elif runtime is not None and not runtime["ready"]:
            reason = runtime["unavailable_reason"]
        candidates = list(review_rule_ids(self._catalog)) if self._catalog is not None else []
        review_reason = reason
        if not self._settings.experimental_review:
            review_reason = {"code": "review_disabled", "message": "Experimental review has not been enabled"}
        elif not configured or self._catalog_error:
            pass
        elif not candidates:
            review_reason = {"code": "review_unavailable", "message": "No supported observation classes are configured"}
        elif not all(tools.values()):
            review_reason = {"code": "video_tools_unavailable", "message": "Local FFmpeg and FFprobe are required"}
        elif runtime is not None and not runtime["ready"]:
            review_reason = runtime["unavailable_reason"]
        else:
            review_reason = None
        return {
            "ready": reason is None,
            "review": {
                "enabled": self._settings.experimental_review, "ready": review_reason is None,
                "rule_ids": candidates, "unavailable_reason": review_reason,
            },
            "analyzer": {
                "configured": configured, "enabled_rule_ids": enabled,
                "runtime": runtime,
                "rules_ready": bool(enabled) and not self._catalog_error,
                "rules": [{"id": rule.id, "event_type": rule.event_type} for rule in self._catalog.rules if rule.enabled]
                if self._catalog else [],
            },
            "tools": tools,
            "limits": {
                "max_upload_bytes": self._settings.max_upload_bytes,
                "max_video_duration_sec": self._settings.max_video_duration_sec,
                "job_timeout_sec": self._settings.job_timeout_sec,
                "upload_timeout_sec": self._settings.upload_timeout_sec,
            },
            "busy": self._runner.busy,
            "unavailable_reason": reason,
        }

    def rules(self) -> dict:
        if self._catalog is None or self._catalog_error:
            raise ServiceError(503, "invalid_rule_catalog", "The local rule catalog is missing or invalid")
        items = [{
            "id": rule.id, "event_type": rule.event_type, "enabled": rule.enabled,
            "verification": rule.verification,
            "verified_on": rule.verified_on.isoformat() if rule.verified_on else None,
            "reference": rule.reference.model_dump(),
            "observable_signals": rule.observable_conditions,
            "required_context": [], "limitations": rule.exclusions,
            "verification_note": rule.verification_note,
        } for rule in self._catalog.rules]
        return {"items": items, "enabled_count": len(self._catalog.enabled_rule_ids), "total": len(items)}

    @asynccontextmanager
    async def admit_upload(self, *, review: bool = False) -> AsyncIterator[_UploadAdmission]:
        # Readiness can contact the loopback model. Do not block HTTP navigation
        # or reserve the upload slot until the bounded check has completed.
        status = await asyncio.to_thread(self.status)
        failure = (status["review"] if review else status)["unavailable_reason"]
        if failure is not None:
            raise ServiceError(503, failure["code"], failure["message"])
        if not self._runner.reserve():
            raise ServiceError(429, "server_busy", "The analyzer is busy; retry after the current job finishes", headers={"Retry-After": "5"})
        admission = _UploadAdmission(self, review=review)
        try:
            yield admission
        finally:
            if not admission._transferred:
                self._runner.release()

    def job(self, job_id: str) -> dict:
        record = self._store.get(_canonical_job_id(job_id))
        if record is None:
            raise ServiceError(404, "job_not_found", "Job not found")
        base = {"job_id": job_id, "status": record["status"], "metadata": record["metadata"]}
        if record["status"] == "done":
            return {**base, **record["result"]}
        if record["status"] == "failed":
            return {**base, "error": {"code": record["error_code"], "message": record["error_message"]}}
        return base

    def history(self, *, limit: int, offset: int, q: str, status: str, verdict: str) -> dict:
        return self._store.list_jobs(limit=limit, offset=offset, q=q, status=status, verdict=verdict)

    def source(self, job_id: str) -> SourceVideo:
        metadata = self._store.get_metadata(_canonical_job_id(job_id))
        if metadata is None:
            raise ServiceError(404, "job_not_found", "Job not found")
        path = self._store.job_dir(job_id) / "input.mov"
        if not path.is_file():
            raise ServiceError(404, "source_unavailable", "The source video is no longer available")
        return SourceVideo(path=path, filename=metadata["filename"])

    def _persist_upload(self, filename: str | None, source: BinaryIO, *, review: bool = False) -> dict:
        filename = _display_name(filename)
        job_id = str(uuid4())
        job_dir = self._store.job_dir(job_id)
        video_path = job_dir / "input.mov"
        directory_created = False
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
            directory_created = True
            size = 0
            with video_path.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > self._settings.max_upload_bytes:
                        raise ServiceError(413, "upload_too_large", "Upload is too large")
                    target.write(chunk)
            if size == 0:
                raise ServiceError(422, "empty_video", "Video file is empty")
            info = self._probe(video_path, self._settings.ffprobe_bin, self._settings.probe_timeout_sec, self._settings.max_video_duration_sec)
            metadata = {"size_bytes": size, **info.as_dict()}
            if review:
                metadata["analysis_mode"] = "experimental_review"
            created_at = self._store.create(job_id, filename, metadata)
        except BaseException as exc:
            if directory_created:
                try:
                    video_path.unlink(missing_ok=True)
                    job_dir.rmdir()
                except OSError:
                    logger.exception("Could not remove an incomplete upload %s", job_id)
            if isinstance(exc, VideoValidationError):
                raise ServiceError(exc.status_code, exc.code, str(exc)) from exc
            if isinstance(exc, (OSError, sqlite3.Error)):
                logger.exception("Could not store video %s", job_id)
                raise ServiceError(507, "storage_unavailable", "The video could not be stored on the local server") from exc
            raise
        return {"job_id": job_id, "status": "processing", "metadata": {
            **metadata, "filename": filename, "created_at": created_at, "updated_at": created_at,
        }}


def _canonical_job_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise ServiceError(404, "job_not_found", "Job not found") from exc
    return value


def _display_name(value: str | None) -> str:
    name = "".join(char for char in PureWindowsPath(value or "").name if char.isprintable())[:255]
    if len(name) <= 4 or not name.lower().endswith(".mov"):
        raise ServiceError(415, "unsupported_file_type", "Only .mov files are accepted")
    return name
