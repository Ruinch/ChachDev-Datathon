from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.video import VideoValidationError, probe_video


def metadata(duration="10", codec_type="video"):
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration},
        "streams": [{"codec_type": codec_type, "codec_name": "h264", "width": 464, "height": 848, "duration": duration}],
    }


def test_probe_reads_mov_video_metadata(monkeypatch):
    def run(command, **kwargs):
        assert command[0] == "ffprobe"
        assert command[-1] == str(Path("source.mov"))
        assert kwargs["timeout"] == 15
        return SimpleNamespace(stdout=json.dumps(metadata()).encode())

    monkeypatch.setattr("app.video.subprocess.run", run)
    info = probe_video(Path("source.mov"), "ffprobe", 15, 180)
    assert info.duration_sec == 10
    assert info.width == 464


@pytest.mark.parametrize("data", [metadata("nan"), metadata("inf"), metadata("0"), metadata("10", "audio"), {"format": {}, "streams": []}])
def test_probe_rejects_invalid_or_audio_only_media(monkeypatch, data):
    monkeypatch.setattr("app.video.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(data).encode()))
    with pytest.raises(VideoValidationError) as error:
        probe_video(Path("source.mov"), "ffprobe", 15, 180)
    assert error.value.code == "invalid_media"


def test_probe_rejects_video_over_duration_limit(monkeypatch):
    monkeypatch.setattr("app.video.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(metadata("181")).encode()))
    with pytest.raises(VideoValidationError) as error:
        probe_video(Path("source.mov"), "ffprobe", 15, 180)
    assert error.value.status_code == 413
    assert error.value.code == "video_too_long"


@pytest.mark.parametrize("exception,code,status", [
    (FileNotFoundError(), "ffprobe_unavailable", 503),
    (subprocess.TimeoutExpired("ffprobe", 15), "video_probe_timeout", 422),
    (subprocess.CalledProcessError(1, "ffprobe"), "invalid_media", 422),
])
def test_probe_classifies_tool_failures(monkeypatch, exception, code, status):
    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr("app.video.subprocess.run", fail)
    with pytest.raises(VideoValidationError) as error:
        probe_video(Path("source.mov"), "ffprobe", 15, 180)
    assert error.value.code == code
    assert error.value.status_code == status
