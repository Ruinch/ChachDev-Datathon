from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


class VideoValidationError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class VideoInfo:
    duration_sec: float
    width: int
    height: int
    codec: str

    def as_dict(self) -> dict:
        return asdict(self)


def executable_available(command: str) -> bool:
    return shutil.which(command) is not None


def probe_video(
    video_path: Path,
    ffprobe_bin: str,
    timeout_sec: float,
    max_duration_sec: float,
) -> VideoInfo:
    """Read actual media metadata locally; filenames and MIME types are untrusted."""
    command = [
        ffprobe_bin,
        "-v", "error",
        "-protocol_whitelist", "file,pipe",
        "-show_entries",
        "format=format_name,duration:stream=codec_type,codec_name,width,height,duration:stream_disposition=attached_pic",
        "-of", "json",
        str(video_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=timeout_sec)
    except FileNotFoundError as exc:
        raise VideoValidationError("ffprobe_unavailable", "Local FFprobe is unavailable", 503) from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoValidationError("video_probe_timeout", "Video validation exceeded its time limit") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VideoValidationError("invalid_media", "The file is not a readable video") from exc

    try:
        data = json.loads(result.stdout)
        container = data.get("format", {})
        # FFprobe uses this demuxer name for the QuickTime/MOV family.
        if "mov" not in container.get("format_name", "").split(","):
            raise ValueError("unsupported container")
        videos = [
            stream for stream in data.get("streams", [])
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic", 0)
        ]
        if not videos:
            raise ValueError("no video stream")
        stream = videos[0]
        width, height = int(stream["width"]), int(stream["height"])
        codec = stream["codec_name"]
        durations = []
        for raw in (stream.get("duration"), container.get("duration")):
            if raw not in (None, "N/A"):
                value = float(raw)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid duration")
                durations.append(value)
        if not durations or width <= 0 or height <= 0 or not codec or codec == "unknown":
            raise ValueError("incomplete video metadata")
        duration = max(durations)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise VideoValidationError("invalid_media", "The file has no usable MOV video stream or duration") from exc

    if duration > max_duration_sec:
        raise VideoValidationError("video_too_long", "Video duration exceeds the configured limit", 413)
    return VideoInfo(duration_sec=duration, width=width, height=height, codec=codec)
