from __future__ import annotations

from dataclasses import dataclass
import importlib
import inspect
import math
from pathlib import Path
import re
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]


class AnalyzerNotConfiguredError(RuntimeError):
    code = "analyzer_unavailable"


class AnalyzerConfigurationError(RuntimeError):
    code = "analyzer_configuration_error"


class AnalysisContractError(RuntimeError):
    code = "invalid_analysis_result"


class InsufficientEvidenceError(AnalysisContractError):
    code = "insufficient_evidence"


class RuleValidationError(AnalysisContractError):
    code = "invalid_rule"


@dataclass(frozen=True)
class AnalysisContext:
    job_id: str
    video_path: Path
    duration_sec: float
    enabled_rule_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not math.isfinite(self.duration_sec) or self.duration_sec <= 0:
            raise AnalysisContractError("The source video must have a finite positive duration")


class DetectedViolation(BaseModel):
    """Visible observations only; the server owns rule text and source frames."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rule_id: str = Field(min_length=1, max_length=64)
    timecode_sec: Timestamp
    description: str = Field(min_length=1, max_length=4000)
    evidence_timestamps_sec: list[Timestamp] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_evidence(self) -> "DetectedViolation":
        if len(set(self.evidence_timestamps_sec)) != len(self.evidence_timestamps_sec):
            raise ValueError("Evidence timestamps must be distinct")
        return self


class LocalAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: Literal["violation", "no_violation"]
    violations: list[DetectedViolation]
    # A missing declaration must never be interpreted as an all-clear result.
    analysis_complete: bool = Field(default=False, strict=True)
    checked_rule_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_verdict(self) -> "LocalAnalysisResult":
        if self.verdict == "violation" and not self.violations:
            raise ValueError("violation requires at least one supported event")
        if self.verdict == "no_violation" and self.violations:
            raise ValueError("no_violation cannot contain violations")
        if len(set(self.checked_rule_ids)) != len(self.checked_rule_ids):
            raise ValueError("checked_rule_ids must be distinct")
        return self


class LocalAnalyzer(Protocol):
    """Runs entirely on the local machine, without network calls."""

    def analyze(self, context: AnalysisContext) -> LocalAnalysisResult: ...


class UnconfiguredAnalyzer:
    def analyze(self, context: AnalysisContext) -> LocalAnalysisResult:
        raise AnalyzerNotConfiguredError(
            "A local video analyzer has not been configured for this server"
        )


def load_analyzer_factory(spec: str | None) -> LocalAnalyzer:
    """Load a trusted local module:factory once inside the analysis worker.

    The factory is a zero-argument callable. It may load local weights, but must
    not download them or make network calls while serving analysis requests.
    """
    if not spec:
        return UnconfiguredAnalyzer()
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", spec):
        raise AnalyzerConfigurationError("APP_ANALYZER_FACTORY must use module:factory syntax")
    module_name, factory_name = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
    except Exception as exc:
        raise AnalyzerConfigurationError("The configured local analyzer factory could not be imported") from exc
    if not callable(factory) or inspect.iscoroutinefunction(factory):
        raise AnalyzerConfigurationError("The configured analyzer factory must be a synchronous callable")
    try:
        analyzer = factory()
    except Exception as exc:
        raise AnalyzerConfigurationError("The local analyzer could not be initialized") from exc
    analyze = getattr(analyzer, "analyze", None)
    if not callable(analyze) or inspect.iscoroutinefunction(analyze):
        raise AnalyzerConfigurationError("The analyzer factory must return an object with callable analyze(context)")
    return analyzer
