"""Unvalidated model suggestions, deliberately separate from verified verdicts."""
from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.analyzer import AnalysisContractError, LocalAnalysisResult, RuleValidationError
from app.evidence import extract_jpeg
from app.ml.rules import RULES
from app.rules import RuleCatalog
from app.schemas import AnalysisScope, EvidenceFrame, RuleReference, Timestamp


REVIEW_LIMITATION = (
    "Экспериментальная модель: возможны ложные срабатывания и пропуски. "
    "Все наблюдения и применимость нормативных пунктов требуют проверки человеком. "
    "Отсутствие наблюдений не подтверждает отсутствие нарушений. "
    "Достоверная численная уверенность модели не откалибрована."
)
INSUFFICIENT_CONTEXT = "Модели не хватило видимого контекста для завершения проверки; нужен ручной просмотр."


def review_rule_ids(catalog: RuleCatalog) -> tuple[str, ...]:
    """Only locally implemented observation classes, never arbitrary catalog IDs."""
    return tuple(rule.id for rule in catalog.rules if rule.id in RULES)


class ReviewObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    type: str
    timecode_sec: Timestamp
    description: str
    evidence_frames: list[EvidenceFrame] = Field(min_length=1, max_length=3)
    candidate_rule: RuleReference
    confidence_status: Literal["uncalibrated"] = "uncalibrated"


class ReviewScope(AnalysisScope):
    mode: Literal["experimental_review"] = "experimental_review"
    full_video_checked: Literal[False] = False
    enabled_rule_ids: list[str] = Field(default_factory=list, max_length=0)
    limitations: list[str] = Field(min_length=1)
    requested_rule_ids: list[str]
    sampled_analysis_complete: bool


class ReviewDoneResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["done"] = "done"
    mode: Literal["experimental_review"] = "experimental_review"
    review_required: Literal[True] = True
    observations: list[ReviewObservation]
    analysis_scope: ReviewScope


def materialize_review(
    job_id: str, video_path: Path, evidence_dir: Path, result: LocalAnalysisResult,
    ffmpeg_bin: str, video_duration_sec: float, rule_catalog: RuleCatalog,
) -> ReviewDoneResponse:
    """Validate timestamps and provenance before extracting any original JPEGs.

    This path never enables a catalog rule or accepts the model's legal verdict.
    Even a model-declared complete, empty scan remains a manual-review result.
    """
    if not math.isfinite(video_duration_sec) or video_duration_sec <= 0:
        raise AnalysisContractError("The source video must have a finite positive duration")
    allowed = review_rule_ids(rule_catalog)
    if not allowed or not set(result.checked_rule_ids).issubset(allowed):
        raise RuleValidationError("The review referenced an unsupported observation class")
    for detected in result.violations:
        if detected.rule_id not in allowed or detected.rule_id not in result.checked_rule_ids:
            raise RuleValidationError("The review referenced an unchecked observation class")
        for timestamp in [detected.timecode_sec, *detected.evidence_timestamps_sec]:
            if not math.isfinite(timestamp) or not 0 <= timestamp < video_duration_sec:
                raise AnalysisContractError("An observation timestamp is outside the source video")

    definitions = {rule.id: rule for rule in rule_catalog.rules}
    observations = []
    for index, detected in enumerate(result.violations):
        frames = []
        for frame_index, timestamp in enumerate(detected.evidence_timestamps_sec):
            jpeg = extract_jpeg(video_path, timestamp, evidence_dir / f"r{index}_f{frame_index}.jpg", ffmpeg_bin)
            frames.append(EvidenceFrame(timestamp_sec=timestamp, image_base64=base64.b64encode(jpeg).decode("ascii")))
        definition = definitions[detected.rule_id]
        observations.append(ReviewObservation(
            rule_id=detected.rule_id, type=definition.event_type,
            timecode_sec=detected.timecode_sec, description=detected.description,
            evidence_frames=frames, candidate_rule=definition.reference,
        ))
    return ReviewDoneResponse(
        job_id=job_id, observations=observations,
        analysis_scope=ReviewScope(
            checked_rule_ids=result.checked_rule_ids, enabled_rule_ids=[],
            requested_rule_ids=list(allowed),
            sampled_analysis_complete=(result.analysis_complete and set(result.checked_rule_ids) == set(allowed) and not result.limitations),
            limitations=list(dict.fromkeys([REVIEW_LIMITATION, *result.limitations])),
        ),
    )
