from __future__ import annotations

import io
import math
import time
from dataclasses import dataclass
from pathlib import Path


class VideoAnalysisError(RuntimeError):
    """Safe error message; never contains frames or model-generated descriptions."""
    code = "local_analysis_failed"


def check_deadline(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VideoAnalysisError("Local analysis time budget exhausted")
    return remaining


@dataclass(frozen=True)
class Frame:
    timestamp: float
    jpeg: bytes


@dataclass(frozen=True)
class Video:
    duration: float
    frames: tuple[Frame, ...]
    decoded_frames: int


def decode_video(path: Path, *, fps: float, max_side: int, deadline: float,
                 max_duration: float = 180) -> Video:
    """Decode only video, retaining presentation timestamps relative to stream start.

    The entire stream is decoded to detect truncation; only sampled JPEGs are kept.
    No audio track is demuxed or decoded. No fixed FPS assumptions are made.
    """
    import av

    if not path.is_file():
        raise VideoAnalysisError("Local video file is missing")
    frames = []
    count = 0
    next_sample = 0.0
    last_time = -1.0
    try:
        with av.open(str(path), options={"protocol_whitelist": "file,pipe"}) as container:
            streams = [s for s in container.streams.video if not (int(s.disposition) & 1024)]
            if not streams:
                raise VideoAnalysisError("No video stream")
            stream = streams[0]
            if stream.width * stream.height > 4096 * 4096:
                raise VideoAnalysisError("Video dimensions exceed local analysis limit")
            declared = float(stream.duration * stream.time_base) if stream.duration else None
            if declared and declared > max_duration:
                raise VideoAnalysisError("Video exceeds configured duration limit")
            origin = float(stream.start_time * stream.time_base) if stream.start_time is not None else None
            frame_step = 1 / float(stream.average_rate) if stream.average_rate else 1 / 30
            for decoded in container.decode(stream):
                check_deadline(deadline)
                if decoded.width * decoded.height > 4096 * 4096:
                    raise VideoAnalysisError("Decoded video dimensions exceed local analysis limit")
                count += 1
                if count > 60000 or decoded.pts is None:
                    raise VideoAnalysisError("Unsupported video timestamps or frame count")
                absolute = float(decoded.pts * decoded.time_base)
                if origin is None:
                    origin = absolute
                timestamp = absolute - origin
                if not math.isfinite(timestamp) or timestamp < -0.001 or timestamp < last_time:
                    raise VideoAnalysisError("Invalid presentation timestamps")
                timestamp = max(timestamp, 0.0)
                last_time = timestamp
                if timestamp > max_duration:
                    raise VideoAnalysisError("Video exceeds configured duration limit")
                if timestamp + 1e-6 >= next_sample:
                    image = decoded.to_image()
                    if decoded.rotation:
                        image = image.rotate(decoded.rotation, expand=True)
                    image.thumbnail((max_side, max_side))
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=85)
                    frames.append(Frame(round(timestamp, 6), output.getvalue()))
                    next_sample = (math.floor(timestamp * fps) + 1) / fps
            if count < 2 or not frames:
                raise VideoAnalysisError("Video has too few decodable frames")
            actual = last_time + frame_step
            if declared and actual < declared - max(0.5, 2 * frame_step):
                raise VideoAnalysisError("Video ended before its declared duration")
            duration = declared or actual
            if frames[-1].timestamp < last_time - 0.01:
                image = decoded.to_image()
                if decoded.rotation:
                    image = image.rotate(decoded.rotation, expand=True)
                image.thumbnail((max_side, max_side))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=85)
                frames.append(Frame(round(last_time, 6), output.getvalue()))
    except VideoAnalysisError:
        raise
    except Exception as exc:
        raise VideoAnalysisError("Local video decoding failed") from exc
    return Video(duration, tuple(frames), count)


def windows(duration: float, length: float, stride: float) -> list[tuple[float, float]]:
    if not all(math.isfinite(x) and x > 0 for x in (duration, length, stride)) or stride > length:
        raise ValueError("Require positive finite duration/length and 0 < stride <= length")
    result = []
    start = 0.0
    while True:
        end = min(start + length, duration)
        result.append((start, end))
        if end >= duration:
            return result
        start += stride


def select_frames(video: Video, start: float, end: float, count: int) -> list[Frame]:
    selected = [f for f in video.frames if start <= f.timestamp <= end]
    if len(selected) <= count:
        return selected
    return [selected[round(i * (len(selected) - 1) / (count - 1))] for i in range(count)]
