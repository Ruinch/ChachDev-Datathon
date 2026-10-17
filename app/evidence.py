from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from app.analyzer import LocalAnalysisResult
from app.schemas import DoneResponse, EvidenceFrame, Violation


class EvidenceExtractionError(RuntimeError):
    pass


def extract_jpeg(video_path: Path, timestamp_sec: float, output_path: Path, ffmpeg_bin: str) -> bytes:
    """Take a real frame from the uploaded video using a local FFmpeg process."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        str(timestamp_sec),
        "-i",
        str(video_path),
        "-frames:v",
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
    if len(frame) < 4 or not (frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")):
        raise EvidenceExtractionError("Evidence frame is not a valid JPEG")
    return frame


def materialize_result(
    job_id: str,
    video_path: Path,
    evidence_dir: Path,
    result: LocalAnalysisResult,
    ffmpeg_bin: str,
) -> DoneResponse:
    violations: list[Violation] = []
    for violation_index, detected in enumerate(result.violations):
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
                type=detected.type,
                timecode_sec=detected.timecode_sec,
                description=detected.description,
                evidence_frames=frames,
                rule=detected.rule,
            )
        )
    return DoneResponse(
        job_id=job_id, verdict=result.verdict, violations=violations
    )
