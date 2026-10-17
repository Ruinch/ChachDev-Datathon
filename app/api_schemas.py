from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import AnalysisScope, DoneResponse, FailedResponse, JobError, ProcessingResponse, RuleReference


class JobMetadata(BaseModel):
    """Older stored jobs may predate probing and have only their filename and dates."""

    model_config = ConfigDict(extra="allow")

    filename: str
    created_at: str
    updated_at: str
    size_bytes: int | None = None
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None


class ProcessingJobResponse(ProcessingResponse):
    metadata: JobMetadata


class DoneJobResponse(DoneResponse):
    metadata: JobMetadata


class FailedJobResponse(FailedResponse):
    metadata: JobMetadata


JobResponse = Annotated[
    ProcessingJobResponse | DoneJobResponse | FailedJobResponse,
    Field(discriminator="status"),
]


class ApiErrorResponse(BaseModel):
    # Missing or malformed multipart fields use FastAPI's standard validation list.
    detail: JobError | list[dict]


class EnabledRuleInfo(BaseModel):
    id: str
    event_type: str


class AnalyzerReadiness(BaseModel):
    configured: bool
    enabled_rule_ids: list[str]
    rules_ready: bool
    rules: list[EnabledRuleInfo]


class MediaToolAvailability(BaseModel):
    ffmpeg: bool
    ffprobe: bool


class SystemLimits(BaseModel):
    max_upload_bytes: int
    max_video_duration_sec: float
    job_timeout_sec: float
    upload_timeout_sec: float


class SystemStatusResponse(BaseModel):
    ready: bool = Field(description="Technical prerequisites are configured; model loading happens inside the analysis worker.")
    analyzer: AnalyzerReadiness
    tools: MediaToolAvailability
    limits: SystemLimits
    busy: bool
    unavailable_reason: JobError | None


class JobHistoryItem(BaseModel):
    job_id: str
    status: Literal["processing", "done", "failed"]
    verdict: Literal["violation", "no_violation"] | None
    event_count: int = Field(ge=0)
    metadata: JobMetadata
    analysis_scope: AnalysisScope | None = None


class JobHistorySummary(BaseModel):
    total: int
    processing: int
    done: int
    failed: int
    violation: int
    no_violation: int


class JobHistoryResponse(BaseModel):
    items: list[JobHistoryItem]
    total: int = Field(description="Number of jobs matching the current filters")
    limit: int
    offset: int
    summary: JobHistorySummary = Field(description="Counts across all jobs, independent of current filters")


class RuleCatalogItem(BaseModel):
    id: str
    event_type: str
    enabled: bool
    verification: Literal["candidate", "verified"]
    verified_on: str | None
    reference: RuleReference
    observable_signals: list[str]
    required_context: list[str]
    limitations: list[str]
    verification_note: str


class RuleCatalogResponse(BaseModel):
    items: list[RuleCatalogItem]
    enabled_count: int
    total: int
