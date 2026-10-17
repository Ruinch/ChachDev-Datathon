from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from typing import Callable
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, Request, Security, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from starlette.types import ASGIApp, Receive, Scope, Send

from app.analyzer import LocalAnalyzer, UnconfiguredAnalyzer
from app.api_schemas import ApiErrorResponse, JobResponse, ProcessingJobResponse, SystemStatusResponse
from app.config import Settings
from app.rules import load_rule_catalog
from app.storage import JobStore
from app.video import VideoInfo, VideoValidationError, executable_available, probe_video
from app.worker import JobRunner


logger = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
API_AUTH = Security(APIKeyHeader(
    name="X-API-Key", scheme_name="ApiKeyAuth", auto_error=False,
    description="Use the jury or local server API key. All /api/v1 endpoints require this header.",
))
AUTH_ERRORS = {401: {"model": ApiErrorResponse, "description": "Missing or invalid API key"}}


def _error(status_code: int, code: str, message: str, headers: dict | None = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message}, headers=headers)


def _job_id_or_404(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise _error(404, "job_not_found", "Job not found") from exc
    if str(parsed) != value:
        raise _error(404, "job_not_found", "Job not found")
    return value


def _safe_display_name(value: str | None) -> str:
    name = PureWindowsPath(value or "").name
    name = "".join(char for char in name if char.isprintable())[:255]
    if len(name) <= 4 or not name.lower().endswith(".mov"):
        raise _error(415, "unsupported_file_type", "Only .mov files are accepted")
    return name


class ApiGuard:
    """Authenticate and reserve capacity before multipart parsing reads an upload."""

    def __init__(self, app: ASGIApp, settings: Settings, runner: JobRunner, status: Callable) -> None:
        self.app, self.settings, self.runner, self.status = app, settings, runner, status

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        given = headers.get(b"x-api-key", b"")
        if not self.settings.api_key or not hmac.compare_digest(given, self.settings.api_key.encode()):
            await JSONResponse(status_code=401, content={"detail": {
                "code": "invalid_api_key", "message": "Invalid API key",
            }})(scope, receive, send)
            return

        is_upload = scope["method"] == "POST" and scope["path"] == "/api/v1/analyze"
        reserved = False
        if is_upload:
            failure = self.status()["unavailable_reason"]
            if failure is not None:
                await JSONResponse(status_code=503, content={"detail": failure})(scope, receive, send)
                return
            raw_length = headers.get(b"content-length")
            if raw_length is not None:
                try:
                    body_size = int(raw_length)
                    if body_size < 0:
                        raise ValueError
                except ValueError:
                    await JSONResponse(status_code=400, content={"detail": {
                        "code": "invalid_content_length", "message": "Invalid Content-Length",
                    }})(scope, receive, send)
                    return
                if body_size > self.settings.max_upload_bytes + 1024 * 1024:
                    await JSONResponse(status_code=413, content={"detail": {
                        "code": "upload_too_large", "message": "Upload is too large",
                    }})(scope, receive, send)
                    return
            reserved = self.runner.reserve()
            if not reserved:
                await JSONResponse(status_code=429, headers={"Retry-After": "5"}, content={"detail": {
                    "code": "server_busy", "message": "The analyzer is busy; retry after the current job finishes",
                }})(scope, receive, send)
                return

        scope.setdefault("state", {})["analysis_slot_transferred"] = False
        received_bytes = 0
        upload_deadline = time.monotonic() + self.settings.upload_timeout_sec

        async def limited_receive():
            nonlocal received_bytes
            if is_upload:
                remaining = upload_deadline - time.monotonic()
                if remaining <= 0:
                    raise _error(408, "upload_timeout", "Upload exceeded its time limit")
                try:
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                except TimeoutError as exc:
                    raise _error(408, "upload_timeout", "Upload exceeded its time limit") from exc
            else:
                message = await receive()
            if is_upload and message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.settings.max_upload_bytes + 1024 * 1024:
                    raise _error(413, "upload_too_large", "Upload is too large")
            return message

        async def private_send(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.extend([(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")])
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, limited_receive, private_send)
        finally:
            if reserved and not scope["state"]["analysis_slot_transferred"]:
                self.runner.release()


def create_app(
    settings: Settings | None = None,
    analyzer: LocalAnalyzer | None = None,
    *,
    probe: Callable[[Path, str, float, float], VideoInfo] = probe_video,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = JobStore(settings.data_dir)
    runner = JobRunner(settings, store, analyzer)
    catalog = None
    catalog_error = False

    def system_status() -> dict:
        configured = bool(settings.analyzer_factory) or (
            analyzer is not None and not isinstance(analyzer, UnconfiguredAnalyzer)
        )
        enabled_ids = list(catalog.enabled_rule_ids) if catalog is not None else []
        tools = {"ffmpeg": executable_available(settings.ffmpeg_bin), "ffprobe": executable_available(settings.ffprobe_bin)}
        reason = None
        if not configured:
            reason = {"code": "analyzer_unavailable", "message": "A local video analyzer has not been configured"}
        elif catalog_error:
            reason = {"code": "invalid_rule_catalog", "message": "The local rule catalog is missing or invalid"}
        elif not enabled_ids:
            reason = {"code": "analysis_not_ready", "message": "No verified rules have been enabled"}
        elif not all(tools.values()):
            reason = {"code": "video_tools_unavailable", "message": "Local FFmpeg and FFprobe are required"}
        return {
            "ready": reason is None,
            "analyzer": {
                "configured": configured, "enabled_rule_ids": enabled_ids,
                "rules_ready": bool(enabled_ids) and not catalog_error,
                "rules": [{"id": rule.id, "event_type": rule.event_type} for rule in catalog.rules if rule.enabled] if catalog else [],
            },
            "tools": tools,
            "limits": {
                "max_upload_bytes": settings.max_upload_bytes,
                "max_video_duration_sec": settings.max_video_duration_sec,
                "job_timeout_sec": settings.job_timeout_sec,
                "upload_timeout_sec": settings.upload_timeout_sec,
            },
            "busy": runner.busy,
            "unavailable_reason": reason,
        }

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal catalog, catalog_error
        settings.validate()
        store.initialize()
        store.acquire_server_lock()
        try:
            store.fail_interrupted()
            try:
                catalog = load_rule_catalog(settings.rule_catalog_path)
                catalog_error = False
            except Exception:
                catalog_error = True
                logger.exception("The rule catalog could not be loaded")
            runner.startup()
            try:
                yield
            finally:
                await asyncio.to_thread(runner.shutdown)
        finally:
            store.release_server_lock()

    app = FastAPI(title="Cash Procedure Audit API", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(ApiGuard, settings=settings, runner=runner, status=system_status)
    app.state.store = store
    app.state.runner = runner

    @app.get("/docs", include_in_schema=False)
    def api_docs(request: Request):
        root_path = request.scope.get("root_path", "").rstrip("/")
        return get_swagger_ui_html(
            openapi_url=f"{root_path}{app.openapi_url}",
            title=f"{app.title} · API",
            swagger_js_url=f"{root_path}/static/vendor/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url=f"{root_path}/static/vendor/swagger-ui/swagger-ui.css",
            swagger_favicon_url=f"{root_path}/static/favicon.svg",
            swagger_ui_parameters={"validatorUrl": None, "persistAuthorization": False},
        )

    @app.get(
        "/api/v1/system", response_model=None, dependencies=[API_AUTH],
        responses={
            **AUTH_ERRORS,
            200: {"model": SystemStatusResponse, "description": "Local analyzer readiness, media tools, enabled rules and processing limits"},
        },
    )
    def get_system():
        return system_status()

    @app.post(
        "/api/v1/analyze", status_code=202, response_model=None, dependencies=[API_AUTH],
        responses={
            **AUTH_ERRORS,
            202: {"model": ProcessingJobResponse, "description": "Video validated and accepted for local analysis"},
            408: {"model": ApiErrorResponse, "description": "Upload exceeded the total receive deadline"},
            413: {"model": ApiErrorResponse, "description": "Video exceeds the size or duration limit"},
            415: {"model": ApiErrorResponse, "description": "Only .mov filenames are accepted"},
            422: {"model": ApiErrorResponse, "description": "Missing file, empty file or unreadable media"},
            429: {"model": ApiErrorResponse, "description": "The single analysis slot is occupied; Retry-After is 5 seconds"},
            503: {"model": ApiErrorResponse, "description": "Analyzer, verified rules or media tools are unavailable"},
            507: {"model": ApiErrorResponse, "description": "Local video storage is unavailable"},
        },
    )
    async def analyze(request: Request, file: UploadFile = File(...)):
        filename = _safe_display_name(file.filename)
        job_id = str(uuid4())
        job_dir = store.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        video_path = job_dir / "input.mov"
        size = 0
        try:
            with video_path.open("xb") as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise _error(413, "upload_too_large", "Upload is too large")
                    target.write(chunk)
            if size == 0:
                raise _error(422, "empty_video", "Video file is empty")
            try:
                info = await asyncio.to_thread(
                    probe, video_path, settings.ffprobe_bin, settings.probe_timeout_sec, settings.max_video_duration_sec,
                )
            except VideoValidationError as exc:
                raise _error(exc.status_code, exc.code, str(exc)) from exc
            store.create(job_id, filename, {"size_bytes": size, **info.as_dict()})
        except BaseException as exc:
            try:
                video_path.unlink(missing_ok=True)
                job_dir.rmdir()
            except OSError:
                logger.exception("Could not remove an incomplete upload %s", job_id)
            if isinstance(exc, OSError):
                raise _error(507, "storage_unavailable", "The video could not be stored on the local server") from exc
            raise
        finally:
            await file.close()
        # The guard now transfers its reservation to the process supervisor.
        request.state.analysis_slot_transferred = True
        runner.start(job_id)
        return {"job_id": job_id, "status": "processing", "metadata": store.get(job_id)["metadata"]}

    def require_job(job_id: str) -> dict:
        record = store.get(_job_id_or_404(job_id))
        if record is None:
            raise _error(404, "job_not_found", "Job not found")
        return record

    @app.get(
        "/api/v1/jobs/{job_id}", response_model=None, dependencies=[API_AUTH],
        responses={
            **AUTH_ERRORS,
            200: {"model": JobResponse, "description": "Processing, completed or failed job with source metadata"},
            404: {"model": ApiErrorResponse, "description": "Job not found"},
        },
    )
    def get_job(job_id: str):
        record = require_job(job_id)
        base = {"job_id": job_id, "status": record["status"], "metadata": record["metadata"]}
        if record["status"] == "done":
            return {**base, **record["result"]}
        if record["status"] == "failed":
            return {**base, "error": {"code": record["error_code"], "message": record["error_message"]}}
        return base

    @app.get(
        "/api/v1/jobs/{job_id}/video", dependencies=[API_AUTH], response_class=FileResponse,
        responses={
            **AUTH_ERRORS,
            200: {"description": "Original MOV video", "content": {"video/quicktime": {"schema": {"type": "string", "format": "binary"}}}},
            206: {"description": "Requested byte range of the original MOV", "content": {"video/quicktime": {"schema": {"type": "string", "format": "binary"}}}},
            404: {"model": ApiErrorResponse, "description": "Job or source video not found"},
            416: {"description": "Requested byte range is invalid"},
        },
    )
    def get_video(job_id: str, download: bool = False):
        record = require_job(job_id)
        path = store.job_dir(job_id) / "input.mov"
        if not path.is_file():
            raise _error(404, "source_unavailable", "The source video is no longer available")
        return FileResponse(
            path, media_type="video/quicktime", filename=record["filename"],
            content_disposition_type="attachment" if download else "inline",
        )

    @app.get("/", include_in_schema=False)
    def index():
        index_file = FRONTEND_DIR / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=404, detail="Frontend has not been installed")
        return FileResponse(index_file)

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR, check_dir=False), name="static")
    return app


app = create_app()
