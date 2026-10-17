from __future__ import annotations

import base64
from io import BytesIO
import math
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.analyzer import AnalysisContractError, InsufficientEvidenceError, LocalAnalysisResult
from app.rules import RuleCatalog, load_rule_catalog
from app.schemas import AnalysisScope, DoneResponse, EvidenceFrame, Violation


class EvidenceExtractionError(RuntimeError):
    code = "evidence_extraction_failed"


def _validate_timestamp(timestamp: float, video_duration_sec: float) -> None:
    if not math.isfinite(timestamp) or not 0 <= timestamp < video_duration_sec:
        raise AnalysisContractError("An event or evidence timestamp is outside the source video")


def validate_analysis_result(
    result: LocalAnalysisResult,
    video_duration_sec: float,
    rule_catalog: RuleCatalog,
) -> None:
    """Require supported observations; accepting an all-clear also needs full coverage."""
    if not math.isfinite(video_duration_sec) or video_duration_sec <= 0:
        raise AnalysisContractError("The source video must have a finite positive duration")
    enabled_ids = set(rule_catalog.enabled_rule_ids)
    if not enabled_ids:
        raise InsufficientEvidenceError("No verified visual rules are enabled for analysis")
    for rule_id in result.checked_rule_ids:
        rule_catalog.require_enabled(rule_id)
    if result.verdict == "no_violation" and not _full_video_checked(result, rule_catalog):
        raise InsufficientEvidenceError(
            "The analyzer could not completely check the video against every enabled rule"
        )
    for detected in result.violations:
        rule_catalog.require_enabled(detected.rule_id)
        if detected.rule_id not in result.checked_rule_ids:
            raise AnalysisContractError("An event references a rule the analyzer did not check")
        _validate_timestamp(detected.timecode_sec, video_duration_sec)
        for timestamp in detected.evidence_timestamps_sec:
            _validate_timestamp(timestamp, video_duration_sec)


def _full_video_checked(result: LocalAnalysisResult, rule_catalog: RuleCatalog) -> bool:
    return (
        result.analysis_complete
        and set(result.checked_rule_ids) == set(rule_catalog.enabled_rule_ids)
        and not result.limitations
    )


def extract_jpeg(video_path: Path, timestamp_sec: float, output_path: Path, ffmpeg_bin: str) -> bytes:
    """Take the source frame displayed at a validated position in the video.

    Preserve frames between the preceding seek point and the requested position.
    Their timestamps become nonpositive after input seeking. A one-microsecond
    output window includes the frame at zero; updating one JPEG leaves the last
    displayed frame, including the final interval before the container ends.
    Passing the filter time base prevents encoder FPS rounding from selecting a
    later frame. No constant frame rate or guessed backwards offset is needed.
    The caller validates the upper bound against the probed source duration.
    """
    if not math.isfinite(timestamp_sec) or timestamp_sec < 0:
        raise AnalysisContractError("An evidence timestamp must be finite and nonnegative")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
    except OSError as exc:
        raise EvidenceExtractionError("Could not prepare an evidence frame") from exc
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        str(timestamp_sec),
        "-noaccurate_seek",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(video_path),
        "-map",
        "0:V:0",
        "-an",
        "-sn",
        "-dn",
        "-t",
        "0.000001",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "filter",
        "-update",
        "1",
        "-vf",
        "scale=960:960:force_original_aspect_ratio=decrease",
        "-q:v",
        "4",
        "-y",
        str(output_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise EvidenceExtractionError("Could not extract an evidence frame") from exc

    try:
        frame = output_path.read_bytes()
    except OSError as exc:
        raise EvidenceExtractionError("FFmpeg did not write an evidence frame") from exc
    try:
        with Image.open(BytesIO(frame)) as image:
            if image.format != "JPEG" or not (0 < image.width <= 960 and 0 < image.height <= 960):
                raise EvidenceExtractionError("Evidence frame must be a JPEG no larger than 960 pixels per side")
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise EvidenceExtractionError("Evidence frame is not a decodable JPEG") from exc
    return frame


def materialize_result(
    job_id: str,
    video_path: Path,
    evidence_dir: Path,
    result: LocalAnalysisResult,
    ffmpeg_bin: str,
    video_duration_sec: float,
    rule_catalog: RuleCatalog | None = None,
) -> DoneResponse:
    rule_catalog = rule_catalog or load_rule_catalog()
    validate_analysis_result(result, video_duration_sec, rule_catalog)
    violations: list[Violation] = []
    for violation_index, detected in enumerate(result.violations):
        rule = rule_catalog.require_enabled(detected.rule_id)
        frames: list[EvidenceFrame] = []
        for frame_index, timestamp in enumerate(detected.evidence_timestamps_sec):
            frame_path = evidence_dir / f"v{violation_index}_f{frame_index}.jpg"
            image_bytes = extract_jpeg(video_path, timestamp, frame_path, ffmpeg_bin)
            frames.append(
                EvidenceFrame(
                    timestamp_sec=timestamp,
                    image_base64=base64.b64encode(image_bytes).decode("ascii"),
                )
            )
        violations.append(
            Violation(
                type=rule.event_type,
                timecode_sec=detected.timecode_sec,
                description=detected.description,
                evidence_frames=frames,
                rule=rule.reference,
            )
        )
    return DoneResponse(
        job_id=job_id,
        verdict=result.verdict,
        violations=violations,
        analysis_scope=AnalysisScope(
            full_video_checked=_full_video_checked(result, rule_catalog),
            checked_rule_ids=result.checked_rule_ids,
            enabled_rule_ids=list(rule_catalog.enabled_rule_ids),
            limitations=result.limitations,
        ),
    )
