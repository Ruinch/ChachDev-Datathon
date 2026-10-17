from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from app.schemas import RuleReference


Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


@dataclass(frozen=True)
class AnalysisContext:
    job_id: str
    video_path: Path


class DetectedViolation(BaseModel):
    """A model observation; the API extracts its own frames from the video."""

    type: str = Field(min_length=1)
    timecode_sec: Timestamp
    description: str = Field(min_length=1)
    evidence_timestamps_sec: list[Timestamp] = Field(min_length=1, max_length=3)
    rule: RuleReference


class LocalAnalysisResult(BaseModel):
    verdict: Literal["violation", "no_violation"]
    violations: list[DetectedViolation]

    @model_validator(mode="after")
    def check_verdict(self) -> "LocalAnalysisResult":
        if self.verdict == "violation" and not self.violations:
            raise ValueError("violation requires at least one supported event")
        if self.verdict == "no_violation" and self.violations:
            raise ValueError("no_violation cannot contain violations")
        return self


class LocalAnalyzer(Protocol):
    """Runs entirely on the local machine, without network calls."""

    def analyze(self, context: AnalysisContext) -> LocalAnalysisResult: ...


class AnalyzerNotConfiguredError(RuntimeError):
    pass


class UnconfiguredAnalyzer:
    def analyze(self, context: AnalysisContext) -> LocalAnalysisResult:
        raise AnalyzerNotConfiguredError(
            "A local video analyzer has not been configured for this server"
        )
