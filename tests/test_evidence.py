from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from app.analyzer import AnalysisContractError, LocalAnalysisResult
from app.evidence import EvidenceExtractionError, extract_jpeg, materialize_result
from app.rules import RuleCatalog


def jpeg_bytes(size=(16, 24)):
    buffer = BytesIO()
    Image.new("RGB", size, color=(20, 50, 90)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.mark.parametrize("frame_bytes", [
    b"\xff\xd8\xff\xd9",  # Markers alone do not prove that a real image exists.
    b"this is not an image",
    jpeg_bytes()[:100],
    jpeg_bytes((961, 10)),
])
def test_rejects_undecodable_or_oversized_extracted_frame(tmp_path, monkeypatch, frame_bytes):
    def fake_ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(frame_bytes)

    monkeypatch.setattr("app.evidence.subprocess.run", fake_ffmpeg)
    with pytest.raises(EvidenceExtractionError):
        extract_jpeg(tmp_path / "input.mov", 1.5, tmp_path / "frame.jpg", "ffmpeg")


def test_extracts_decodable_jpeg_and_ignores_audio(tmp_path, monkeypatch):
    frame = jpeg_bytes()

    def fake_ffmpeg(command, **kwargs):
        assert command[command.index("-ss") + 1] == "1.5"
        assert command[command.index("-map") + 1] == "0:V:0"
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command.index("-protocol_whitelist") < command.index("-i")
        assert "-an" in command and "-sn" in command
        Path(command[-1]).write_bytes(frame)

    monkeypatch.setattr("app.evidence.subprocess.run", fake_ffmpeg)
    assert extract_jpeg(tmp_path / "input.mov", 1.5, tmp_path / "frame.jpg", "ffmpeg") == frame


@pytest.mark.parametrize("timestamp", [-1, float("nan"), float("inf")])
def test_invalid_timestamp_is_rejected_before_extracting(tmp_path, monkeypatch, timestamp):
    def must_not_run(*args, **kwargs):
        pytest.fail("Invalid timestamps must not reach FFmpeg")

    monkeypatch.setattr("app.evidence.subprocess.run", must_not_run)
    with pytest.raises(AnalysisContractError):
        extract_jpeg(tmp_path / "input.mov", timestamp, tmp_path / "frame.jpg", "ffmpeg")


def test_empty_extraction_cannot_reuse_an_old_evidence_image(tmp_path, monkeypatch):
    output = tmp_path / "frame.jpg"
    output.write_bytes(jpeg_bytes())
    monkeypatch.setattr("app.evidence.subprocess.run", lambda *args, **kwargs: None)
    with pytest.raises(EvidenceExtractionError):
        extract_jpeg(tmp_path / "input.mov", 1.5, output, "ffmpeg")


def test_materialized_event_uses_catalog_and_original_frame_bytes(tmp_path, monkeypatch):
    catalog = RuleCatalog.model_validate({
        "version": "synthetic-test-only",
        "rules": [{
            "id": "TEST-RULE", "event_type": "Catalog event", "enabled": True,
            "verification": "verified", "verified_on": "2026-10-17",
            "reference": {"title": "Synthetic", "clause": "1", "source_url": "https://example.com/test"},
            "observable_conditions": ["Test condition"], "exclusions": ["Occluded"],
            "verification_note": "Synthetic fixture only",
        }],
    })
    result = LocalAnalysisResult.model_validate({
        "verdict": "violation", "analysis_complete": True, "checked_rule_ids": ["TEST-RULE"],
        "violations": [{
            "rule_id": "TEST-RULE", "timecode_sec": 2.5, "description": "Observed test event",
            "evidence_timestamps_sec": [2.0, 2.5],
        }],
    })
    frame = jpeg_bytes()
    observed_timestamps = []

    def source_frame(video_path, timestamp_sec, output_path, ffmpeg_bin):
        assert video_path == tmp_path / "source.mov"
        observed_timestamps.append(timestamp_sec)
        return frame

    monkeypatch.setattr("app.evidence.extract_jpeg", source_frame)
    response = materialize_result("job", tmp_path / "source.mov", tmp_path / "frames",
                                  result, "ffmpeg", 5.0, catalog)
    event = response.violations[0]
    assert observed_timestamps == [2.0, 2.5]
    assert event.type == "Catalog event"
    assert event.rule == catalog.rules[0].reference
    assert base64.b64decode(event.evidence_frames[0].image_base64) == frame
    assert response.analysis_scope.model_dump() == {
        "full_video_checked": True,
        "checked_rule_ids": ["TEST-RULE"],
        "enabled_rule_ids": ["TEST-RULE"],
        "limitations": [],
    }
