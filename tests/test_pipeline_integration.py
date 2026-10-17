"""Real media smoke checks. Uses generated test patterns, never the bank dataset."""

from __future__ import annotations

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from fastapi.testclient import TestClient
from PIL import Image, ImageStat
import pytest

from app.analyzer import DetectedViolation, LocalAnalysisResult
from app.config import Settings
from app.evidence import EvidenceExtractionError, extract_jpeg
from app.main import create_app


KEY = "synthetic-integration-test-only"
HEADERS = {"X-API-Key": KEY}


class SyntheticEventsAnalyzer:
    """Deterministic test fixture; does not perform or claim ML inference."""

    def analyze(self, context):
        return LocalAnalysisResult(
            verdict="violation",
            analysis_complete=True,
            checked_rule_ids=list(context.enabled_rule_ids),
            violations=[
                DetectedViolation(
                    rule_id=context.enabled_rule_ids[0],
                    timecode_sec=timestamp,
                    description="Synthetic pattern for exercising the service contract only",
                    evidence_timestamps_sec=[timestamp, timestamp + .1],
                )
                for timestamp in (.4, 1.2)
            ],
        )


class SyntheticNoEventsAnalyzer:
    def analyze(self, context):
        return LocalAnalysisResult(
            verdict="no_violation", violations=[], analysis_complete=True,
            checked_rule_ids=list(context.enabled_rule_ids),
        )


class SyntheticNearEndAnalyzer:
    def analyze(self, context):
        return LocalAnalysisResult(
            verdict="violation", analysis_complete=True,
            checked_rule_ids=list(context.enabled_rule_ids),
            violations=[DetectedViolation(
                rule_id=context.enabled_rule_ids[0], timecode_sec=1.99,
                description="Synthetic last display interval only",
                evidence_timestamps_sec=[1.95, 1.99],
            )],
        )


@pytest.fixture(scope="module")
def media_tools():
    ffmpeg = shutil.which(os.getenv("APP_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.getenv("APP_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Real media smoke requires APP_FFMPEG_BIN and APP_FFPROBE_BIN or PATH tools")
    return ffmpeg, ffprobe


@pytest.fixture
def synthetic_video(tmp_path, media_tools):
    path = tmp_path / "synthetic.mov"
    subprocess.run([
        media_tools[0], "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
        "-movflags", "+faststart", str(path),
    ], check=True, capture_output=True, timeout=20)
    return path


@pytest.fixture
def media_settings(tmp_path, media_tools):
    catalog = tmp_path / "synthetic-rules.json"
    catalog.write_text(json.dumps({"version": "synthetic-test", "rules": [{
        "id": "SYNTHETIC-RULE", "event_type": "Synthetic test pattern",
        "enabled": True, "verification": "verified", "verified_on": "2026-10-17",
        "reference": {"title": "Test fixture, not a legal rule", "clause": "test",
                      "source_url": "https://example.org/synthetic-test"},
        "observable_conditions": ["Generated color pattern"],
        "exclusions": ["All real-world banking footage"],
        "verification_note": "Only for deterministic integration tests",
    }]}), encoding="utf-8")
    return Settings(
        api_key=KEY, data_dir=tmp_path / "jobs-data",
        ffmpeg_bin=media_tools[0], ffprobe_bin=media_tools[1],
        rule_catalog_path=catalog, job_timeout_sec=20,
    )


def await_result(client, job_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get("/api/v1/jobs/" + job_id, headers=HEADERS)
        assert response.status_code == 200
        result = response.json()
        if result["status"] != "processing":
            return result
        time.sleep(.05)
    raise AssertionError("Synthetic integration job did not finish")


def test_real_video_multi_event_frames_and_authenticated_source(media_settings, synthetic_video):
    source_bytes = synthetic_video.read_bytes()
    with TestClient(create_app(media_settings, analyzer=SyntheticEventsAnalyzer())) as client:
        assert client.get("/api/v1/system", headers=HEADERS).json()["ready"]
        created = client.post("/api/v1/analyze", headers=HEADERS,
                              files={"file": ("synthetic.mov", source_bytes, "video/quicktime")})
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        result = await_result(client, job_id)
        assert result["status"] == "done", result
        assert result["verdict"] == "violation"
        assert len(result["violations"]) == 2
        assert result["metadata"]["duration_sec"] == pytest.approx(2, abs=.05)
        assert result["analysis_scope"]["full_video_checked"] is True
        assert all(event["type"] == "Synthetic test pattern" for event in result["violations"])
        frames = [frame for event in result["violations"] for frame in event["evidence_frames"]]
        assert len(frames) == 4
        for frame in frames:
            with Image.open(BytesIO(base64.b64decode(frame["image_base64"], validate=True))) as image:
                assert image.format == "JPEG"
                assert max(image.size) <= 960
                assert max(ImageStat.Stat(image).var) > 10
        assert frames[0]["image_base64"] != frames[-1]["image_base64"]
        path = "/api/v1/jobs/" + job_id + "/video"
        assert client.get(path).status_code == 401
        source = client.get(path, headers=HEADERS)
        assert source.content == source_bytes
        assert source.headers["cache-control"] == "no-store"
        part = client.get(path, headers={**HEADERS, "Range": "bytes=0-31"})
        assert part.status_code == 206
        assert part.content == source_bytes[:32]


def test_real_video_no_events_has_explicit_complete_scope(media_settings, synthetic_video):
    with TestClient(create_app(media_settings, analyzer=SyntheticNoEventsAnalyzer())) as client:
        created = client.post("/api/v1/analyze", headers=HEADERS,
                              files={"file": ("synthetic.mov", synthetic_video.read_bytes())})
        assert created.status_code == 202
        result = await_result(client, created.json()["job_id"])
        assert result["status"] == "done", result
        assert result["verdict"] == "no_violation"
        assert result["violations"] == []
        assert result["analysis_scope"] == {
            "full_video_checked": True, "checked_rule_ids": ["SYNTHETIC-RULE"],
            "enabled_rule_ids": ["SYNTHETIC-RULE"], "limitations": [],
        }


@pytest.mark.parametrize("timestamp", [1.95, 1.99])
def test_last_display_interval_uses_last_existing_source_frame(
    synthetic_video, media_tools, tmp_path, timestamp,
):
    # The final frame starts at 1.9 and remains displayed until duration 2.0.
    expected = extract_jpeg(synthetic_video, 1.9, tmp_path / "last.jpg", media_tools[0])
    actual = extract_jpeg(synthetic_video, timestamp, tmp_path / "selected.jpg", media_tools[0])
    assert actual == expected


@pytest.mark.parametrize("timestamp", [0, .4, .49])
def test_normal_position_uses_the_displayed_frame_not_a_future_frame(
    synthetic_video, media_tools, tmp_path, timestamp,
):
    frame_start = 0 if timestamp == 0 else .4
    expected = extract_jpeg(synthetic_video, frame_start, tmp_path / "exact.jpg", media_tools[0])
    actual = extract_jpeg(synthetic_video, timestamp, tmp_path / "selected.jpg", media_tools[0])
    assert actual == expected


def test_final_display_interval_with_variable_rate_and_nonzero_source_start(
    tmp_path, media_tools,
):
    source = tmp_path / "variable-start.mov"
    subprocess.run([
        media_tools[0], "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=1.3",
        "-vf", "select='eq(n,0)+eq(n,1)+eq(n,4)+eq(n,12)'",
        "-fps_mode", "vfr", "-enc_time_base", "1:10", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-an", "-output_ts_offset", "5", str(source),
    ], check=True, capture_output=True, timeout=20)
    probed = subprocess.run([
        media_tools[1], "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(source),
    ], check=True, capture_output=True, timeout=10)
    pts = [float(frame["best_effort_timestamp_time"])
           for frame in json.loads(probed.stdout)["frames"]]
    assert pts == pytest.approx([5, 5.1, 5.4, 6.2])
    expected = extract_jpeg(source, 1.2, tmp_path / "exact-last.jpg", media_tools[0])
    actual = extract_jpeg(source, 1.29, tmp_path / "during-last.jpg", media_tools[0])
    assert actual == expected


def test_real_video_near_end_event_finishes_and_keeps_requested_display_times(
    media_settings, synthetic_video, media_tools, tmp_path,
):
    expected = extract_jpeg(synthetic_video, 1.9, tmp_path / "last.jpg", media_tools[0])
    with TestClient(create_app(media_settings, analyzer=SyntheticNearEndAnalyzer())) as client:
        accepted = client.post("/api/v1/analyze", headers=HEADERS,
                               files={"file": ("synthetic.mov", synthetic_video.read_bytes())})
        assert accepted.status_code == 202, accepted.text
        result = await_result(client, accepted.json()["job_id"])
        assert result["status"] == "done", result
        event = result["violations"][0]
        assert event["timecode_sec"] == 1.99
        assert [frame["timestamp_sec"] for frame in event["evidence_frames"]] == [1.95, 1.99]
        assert all(base64.b64decode(frame["image_base64"]) == expected
                   for frame in event["evidence_frames"])


def test_real_corrupt_source_does_not_get_an_evidence_frame(tmp_path, media_tools):
    source = tmp_path / "corrupt.mov"
    source.write_bytes(b"not a readable video")
    with pytest.raises(EvidenceExtractionError):
        extract_jpeg(source, 1.99, tmp_path / "corrupt.jpg", media_tools[0])


def test_actual_ffprobe_rejects_a_renamed_text_file(media_settings):
    with TestClient(create_app(media_settings, analyzer=SyntheticNoEventsAnalyzer())) as client:
        response = client.post("/api/v1/analyze", headers=HEADERS,
                               files={"file": ("not-video.mov", b"this is not a video")})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_media"
        assert not list((media_settings.data_dir / "jobs").iterdir())
