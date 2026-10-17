from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.analyzer import (
    AnalysisContext,
    AnalyzerNotConfiguredError,
    LocalAnalysisResult,
    LocalAnalyzer,
    UnconfiguredAnalyzer,
)
from app.config import Settings
from app.evidence import EvidenceExtractionError, materialize_result
from app.schemas import FailedResponse, JobError, ProcessingResponse
from app.storage import JobStore


logger = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _job_id_or_404(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    if str(parsed) != value:
        raise HTTPException(status_code=404, detail="Job not found")
    return value


def _safe_display_name(value: str | None) -> str:
    name = PureWindowsPath(value or "").name
    name = "".join(char for char in name if char.isprintable())[:255]
    if len(name) <= 4 or not name.lower().endswith(".mov"):
        raise HTTPException(status_code=415, detail="Only .mov files are accepted")
    return name


def _process_job(job_id: str, store: JobStore, analyzer: LocalAnalyzer, ffmpeg_bin: str) -> None:
    video_path = store.job_dir(job_id) / "input.mov"
    try:
        detected = LocalAnalysisResult.model_validate(
            analyzer.analyze(AnalysisContext(job_id=job_id, video_path=video_path))
        )
        completed = materialize_result(
            job_id=job_id,
            video_path=video_path,
            evidence_dir=store.evidence_dir(job_id),
            result=detected,
            ffmpeg_bin=ffmpeg_bin,
        )
        store.set_done(
            job_id,
            completed.model_dump(mode="json", exclude={"job_id", "status"}),
        )
    except AnalyzerNotConfiguredError as exc:
        store.set_failed(job_id, "analyzer_unavailable", str(exc))
    except EvidenceExtractionError:
        logger.exception("Evidence extraction failed for job %s", job_id)
        store.set_failed(
            job_id,
            "evidence_extraction_failed",
            "Could not extract evidence frames from the uploaded video",
        )
    except Exception:
        logger.exception("Local analysis failed for job %s", job_id)
        store.set_failed(job_id, "analysis_failed", "Local video analysis failed")


def create_app(
    settings: Settings | None = None,
    analyzer: LocalAnalyzer | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    analyzer = analyzer or UnconfiguredAnalyzer()
    store = JobStore(settings.data_dir)
    ffmpeg_bin = os.getenv("APP_FFMPEG_BIN", "ffmpeg")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.validate()
        store.initialize()
        store.fail_interrupted()
        yield

    app = FastAPI(title="Cash Procedure Audit API", lifespan=lifespan)

    @app.middleware("http")
    async def protect_api(request: Request, call_next):
        if request.url.path.startswith("/api/v1/"):
            given = request.headers.get("X-API-Key", "")
            if not settings.api_key or not hmac.compare_digest(given, settings.api_key):
                return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
            if request.method == "POST" and request.url.path == "/api/v1/analyze":
                content_length = request.headers.get("content-length")
                if content_length:
                    try:
                        body_size = int(content_length)
                    except ValueError:
                        return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
                    if body_size > settings.max_upload_bytes + 1024 * 1024:
                        return JSONResponse(status_code=413, content={"detail": "Upload is too large"})
        return await call_next(request)

    @app.post("/api/v1/analyze", status_code=202, response_model=ProcessingResponse)
    async def analyze(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
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
                        raise HTTPException(status_code=413, detail="Upload is too large")
                    target.write(chunk)
            if size == 0:
                raise HTTPException(status_code=422, detail="Video file is empty")
            store.create(job_id, filename)
        except Exception:
            video_path.unlink(missing_ok=True)
            job_dir.rmdir()
            raise
        finally:
            await file.close()
        background_tasks.add_task(_process_job, job_id, store, analyzer, ffmpeg_bin)
        return ProcessingResponse(job_id=job_id)

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str):
        record = store.get(_job_id_or_404(job_id))
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if record["status"] == "done":
            return {"job_id": job_id, "status": "done", **record["result"]}
        if record["status"] == "failed":
            return FailedResponse(
                job_id=job_id,
                error=JobError(code=record["error_code"], message=record["error_message"]),
            )
        return ProcessingResponse(job_id=job_id)

    @app.get("/", include_in_schema=False)
    def index():
        index_file = FRONTEND_DIR / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=404, detail="Frontend has not been installed")
        return FileResponse(index_file)

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR, check_dir=False), name="static")
    return app


app = create_app()
