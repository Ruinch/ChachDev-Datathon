from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


Verdict = Literal["violation", "no_violation"]
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RuleReference(BaseModel):
    title: str = Field(min_length=1)
    clause: str = Field(min_length=1)
    source_url: str = Field(min_length=1)


class EvidenceFrame(BaseModel):
    timestamp_sec: Timestamp
    image_base64: str = Field(min_length=1)


class Violation(BaseModel):
    type: str = Field(min_length=1)
    timecode_sec: Timestamp
    description: str = Field(min_length=1)
    evidence_frames: list[EvidenceFrame] = Field(min_length=1, max_length=3)
    rule: RuleReference


class DoneResponse(BaseModel):
    job_id: str
    status: Literal["done"] = "done"
    verdict: Verdict
    violations: list[Violation]

    @model_validator(mode="after")
    def check_verdict(self) -> "DoneResponse":
        if self.verdict == "violation" and not self.violations:
            raise ValueError("violation requires at least one supported event")
        if self.verdict == "no_violation" and self.violations:
            raise ValueError("no_violation cannot contain violations")
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
