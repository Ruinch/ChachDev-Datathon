"""HTTP representation and documentation of the local job module."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Query, Request, Security, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader

from app.api_schemas import ApiErrorResponse, JobHistoryResponse, JobResponse, ProcessingJobResponse, RuleCatalogResponse, SystemStatusResponse
from app.job_service import JobService, ServiceError
from app.legal_guide import LegalGuide, LegalGuideUnavailableError, load_legal_guide


API_AUTH = Security(APIKeyHeader(
    name="X-API-Key", scheme_name="ApiKeyAuth", auto_error=False,
    description="Use the jury or local server API key. All /api/v1 endpoints require this header.",
))
AUTH_ERRORS = {401: {"model": ApiErrorResponse, "description": "Missing or invalid API key"}}


def api_router(service: JobService) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/v1/system", response_model=None, dependencies=[API_AUTH],
        responses={
            **AUTH_ERRORS,
            200: {"model": SystemStatusResponse, "description": "Local analyzer readiness, media tools, enabled rules and processing limits"},
        },
    )
    def get_system():
        return service.status()

    @router.get(
        "/api/v1/jobs", response_model=None, dependencies=[API_AUTH],
        responses={
            **AUTH_ERRORS,
            200: {"model": JobHistoryResponse, "description": "Lightweight job history, newest first; no evidence images or event descriptions"},
        },
    )
    def list_jobs(
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        q: str = Query("", max_length=255, description="Case-insensitive literal filename substring"),
        status: Literal["all", "processing", "done", "failed"] = "all",
        verdict: Literal["all", "violation", "no_violation"] = "all",
    ):
        return service.history(limit=limit, offset=offset, q=q, status=status, verdict=verdict)

    @router.get(
        "/api/v1/rules", response_model=None, dependencies=[API_AUTH],
        responses={
            **AUTH_ERRORS,
            200: {"model": RuleCatalogResponse, "description": "Local rule catalog including disabled candidates and their verification state"},
            503: {"model": ApiErrorResponse, "description": "The local rule catalog is missing or invalid"},
        },
    )
    def get_rules():
        return service.rules()

    @router.get(
        "/api/v1/legal-guide", response_model=None, dependencies=[API_AUTH],
        responses={
            **AUTH_ERRORS,
            200: {"model": LegalGuide, "description": "Current explanatory legal guide; does not activate analysis rules or establish a historical result binding"},
            503: {"model": ApiErrorResponse, "description": "The local legal guide is missing or invalid"},
        },
    )
    def get_legal_guide():
        try:
            return load_legal_guide().model_dump(mode="json")
        except LegalGuideUnavailableError as exc:
            raise ServiceError(503, "invalid_legal_guide", "The local legal guide is missing or invalid") from exc

    @router.post(
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
        try:
            return await request.state.upload_admission.accept(file.filename, file.file)
        finally:
            await file.close()

    @router.get(
        "/api/v1/jobs/{job_id}", response_model=None, dependencies=[API_AUTH],
        responses={
            **AUTH_ERRORS,
            200: {"model": JobResponse, "description": "Processing, completed or failed job with source metadata"},
            404: {"model": ApiErrorResponse, "description": "Job not found"},
        },
    )
    def get_job(job_id: str):
        return service.job(job_id)

    @router.get(
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
        source = service.source(job_id)
        return FileResponse(
            source.path, media_type="video/quicktime", filename=source.filename,
            content_disposition_type="attachment" if download else "inline",
        )

    return router
