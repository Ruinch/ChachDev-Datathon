from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from urllib.parse import urlparse


Verdict = Literal["violation", "no_violation"]
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RuleReference(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1)
    clause: str = Field(min_length=1)
    source_url: str = Field(min_length=1)

    @field_validator("source_url")
    @classmethod
    def source_must_be_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("A normative source must be an HTTPS URL without credentials")
        return value


class EvidenceFrame(BaseModel):
    timestamp_sec: Timestamp
    image_base64: str = Field(min_length=1)


class Violation(BaseModel):
    type: str = Field(min_length=1)
    timecode_sec: Timestamp
    description: str = Field(min_length=1)
    evidence_frames: list[EvidenceFrame] = Field(min_length=1, max_length=3)
    rule: RuleReference


class AnalysisScope(BaseModel):
    full_video_checked: bool
    checked_rule_ids: list[str]
    enabled_rule_ids: list[str]
    limitations: list[str]

    @model_validator(mode="after")
    def full_check_requires_complete_scope(self) -> "AnalysisScope":
        if self.full_video_checked and (
            not self.enabled_rule_ids
            or set(self.checked_rule_ids) != set(self.enabled_rule_ids)
            or self.limitations
        ):
            raise ValueError("A full video check requires every enabled rule and no unresolved limitations")
        return self


class DoneResponse(BaseModel):
    job_id: str
    status: Literal["done"] = "done"
    verdict: Verdict
    violations: list[Violation]
    analysis_scope: AnalysisScope

    @model_validator(mode="after")
    def check_verdict(self) -> "DoneResponse":
        if self.verdict == "violation" and not self.violations:
            raise ValueError("violation requires at least one supported event")
        if self.verdict == "no_violation" and self.violations:
            raise ValueError("no_violation cannot contain violations")
        if self.verdict == "no_violation" and not self.analysis_scope.full_video_checked:
            raise ValueError("no_violation requires a full video check")
        return self


class ProcessingResponse(BaseModel):
    job_id: str
    status: Literal["processing"] = "processing"


class JobError(BaseModel):
    code: str
    message: str


class FailedResponse(BaseModel):
    job_id: str
    status: Literal["failed"] = "failed"
    error: JobError
