"""Experimental HTTP review stays distinct from accepted violation analysis.

All inputs are generated test media and synthetic candidate rules. The fake
analyzer is the existing process-safe integration seam, never a quality test.
"""
from __future__ import annotations

import base64
from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.analyzer import InsufficientEvidenceError, LocalAnalysisResult
from app.config import Settings
from app.main import create_app


HEADERS = {"X-API-Key": "synthetic-review-test-key"}
RULE_IDS = ["R20-A", "R20-B"]


class ReviewAnalyzer:
    def __init__(self, outcome="events"):
        self.outcome = outcome

    def analyze(self, context):
        assert list(context.enabled_rule_ids) == RULE_IDS
        if self.outcome == "sleep":
            time.sleep(30)
        if self.outcome == "insufficient":
            raise InsufficientEvidenceError("Synthetic context is not visible")
        events = []
        if self.outcome not in {"empty", "incomplete", "sleep"}:
            events = [{
                "rule_id": rule_id, "timecode_sec": timestamp,
                "description": "Synthetic unconfirmed observation",
                "evidence_timestamps_sec": [timestamp],
            } for rule_id, timestamp in zip(RULE_IDS, (0.5, 1.5))]
        if self.outcome == "unknown_rule":
            events[0]["rule_id"] = "UNKNOWN-RULE"
        elif self.outcome == "outside_time":
            events[0]["timecode_sec"] = context.duration_sec
        elif self.outcome == "outside_evidence":
            events[0]["evidence_timestamps_sec"] = [context.duration_sec]
        return LocalAnalysisResult(
            verdict="violation" if events else "no_violation", violations=events,
            analysis_complete=self.outcome != "incomplete",
            checked_rule_ids=RULE_IDS[:1] if self.outcome == "unchecked_rule" else RULE_IDS,
            limitations=["Synthetic incomplete context"] if self.outcome == "incomplete" else [],
        )


def _tool(name):
    configured = os.getenv("APP_" + name.upper() + "_BIN")
    local = Path(__file__).resolve().parents[1] / ".local_tools" / "ffmpeg" / "bin" / (name + ".exe")
    found = configured or (str(local) if local.is_file() else shutil.which(name))
    if not found:
        pytest.skip(f"Real {name} is required for synthetic HTTP media tests")
    return found


@pytest.fixture
def review_settings(tmp_path):
    catalog = tmp_path / "candidate-rules.json"
    catalog.write_text(json.dumps({"version": "synthetic-review-only", "rules": [{
        "id": rule_id, "event_type": "Synthetic candidate " + rule_id,
        "enabled": False, "verification": "candidate", "verified_on": None,
        "reference": {"title": "Synthetic reference only", "clause": rule_id,
                      "source_url": "https://example.com/synthetic/" + rule_id},
        "observable_conditions": ["Synthetic visible condition"],
        "exclusions": ["Synthetic exclusion"], "verification_note": "Not accepted for analysis",
    } for rule_id in RULE_IDS]}), encoding="utf-8")
    return Settings(
        api_key=HEADERS["X-API-Key"], data_dir=tmp_path / "data",
        rule_catalog_path=catalog, ffmpeg_bin=_tool("ffmpeg"), ffprobe_bin=_tool("ffprobe"),
        experimental_review=True,
    )


@pytest.fixture
def synthetic_mov(tmp_path):
    output = tmp_path / "synthetic.mov"
    subprocess.run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "color=c=red:s=96x64:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
    ], check=True, capture_output=True, timeout=15)
    return output.read_bytes()


def _upload(client, source, *, route="review", headers=HEADERS):
    return client.post("/api/v1/" + route, headers=headers,
                       files={"file": ("synthetic.mov", source, "video/quicktime")})


def _wait(client, job_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}", headers=HEADERS)
        assert response.status_code == 200
        result = response.json()
        if result["status"] != "processing":
            return result
        time.sleep(0.05)
    pytest.fail("The synthetic review did not reach a terminal state")


def _assert_review_only(result):
    assert result["status"] == "done", result.get("error")
    assert result["mode"] == "experimental_review"
    assert result["review_required"] is True
    assert "verdict" not in result
    assert "violations" not in result
    scope = result["analysis_scope"]
    assert scope["mode"] == "experimental_review"
    assert scope["full_video_checked"] is False
    assert scope["enabled_rule_ids"] == []
    assert scope["requested_rule_ids"] == RULE_IDS
    assert scope["limitations"]
    assert result["metadata"]["analysis_mode"] == "experimental_review"


def test_review_is_opt_in_and_never_enables_strict_analysis(review_settings):
    for enabled in (False, True):
        settings = replace(review_settings, experimental_review=enabled)
        with TestClient(create_app(settings, analyzer=ReviewAnalyzer())) as client:
            status = client.get("/api/v1/system", headers=HEADERS).json()
            assert status["ready"] is False
            assert status["analyzer"]["enabled_rule_ids"] == []
            assert status["review"]["enabled"] is enabled
            assert status["review"]["ready"] is enabled
            assert client.post("/api/v1/analyze", headers=HEADERS).status_code == 503
            if not enabled:
                assert client.post("/api/v1/review", headers=HEADERS).status_code == 503
            rules = client.get("/api/v1/rules", headers=HEADERS).json()
            assert rules["enabled_count"] == 0
            assert all(not item["enabled"] and item["verification"] == "candidate" for item in rules["items"])
            assert client.get("/api/v1/jobs", headers=HEADERS).json()["total"] == 0


def test_observations_have_original_jpegs_and_survive_reopening(review_settings, synthetic_mov):
    with TestClient(create_app(review_settings, analyzer=ReviewAnalyzer())) as client:
        accepted = _upload(client, synthetic_mov)
        assert accepted.status_code == 202, accepted.json()
        job_id = accepted.json()["job_id"]
        result = _wait(client, job_id)
        _assert_review_only(result)
        assert result["analysis_scope"]["sampled_analysis_complete"] is True
        assert len(result["observations"]) == 2
        for index, event in enumerate(result["observations"]):
            assert event["rule_id"] == RULE_IDS[index]
            assert event["confidence_status"] == "uncalibrated"
            assert "rule" not in event
            assert event["candidate_rule"]["source_url"] == "https://example.com/synthetic/" + RULE_IDS[index]
            frame = event["evidence_frames"][0]
            assert frame["timestamp_sec"] == (0.5, 1.5)[index]
            with Image.open(BytesIO(base64.b64decode(frame["image_base64"], validate=True))) as image:
                assert image.format == "JPEG"
                red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
                assert green < 40
                if index == 0:
                    assert red > 200 and blue < 40
                else:
                    assert blue > 200 and red < 40
        assert client.get(f"/api/v1/jobs/{job_id}/video", headers=HEADERS).content == synthetic_mov
        history = client.get("/api/v1/jobs", headers=HEADERS).json()
        assert history["summary"]["done"] == 1
        assert history["summary"]["violation"] == history["summary"]["no_violation"] == 0
        assert history["items"][0]["verdict"] is None
        assert history["items"][0]["event_count"] == 2
        assert history["items"][0]["metadata"]["analysis_mode"] == "experimental_review"

    with TestClient(create_app(review_settings, analyzer=ReviewAnalyzer())) as restarted:
        saved = restarted.get(f"/api/v1/jobs/{job_id}", headers=HEADERS).json()
        assert saved == result
        _assert_review_only(saved)


@pytest.mark.parametrize("outcome,complete", [("empty", True), ("incomplete", False), ("insufficient", False)])
def test_no_observations_or_missing_context_never_means_all_clear(review_settings, synthetic_mov, outcome, complete):
    with TestClient(create_app(review_settings, analyzer=ReviewAnalyzer(outcome))) as client:
        accepted = _upload(client, synthetic_mov)
        assert accepted.status_code == 202
        result = _wait(client, accepted.json()["job_id"])
        _assert_review_only(result)
        assert result["observations"] == []
        assert result["analysis_scope"]["sampled_analysis_complete"] is complete
        if outcome == "insufficient":
            assert result["analysis_scope"]["checked_rule_ids"] == []
        history = client.get("/api/v1/jobs", headers=HEADERS).json()
        assert history["summary"]["done"] == 1
        assert history["summary"]["no_violation"] == 0
        assert history["items"][0]["verdict"] is None
        assert history["items"][0]["event_count"] == 0


@pytest.mark.parametrize("outcome,code", [
    ("unknown_rule", "invalid_rule"), ("unchecked_rule", "invalid_rule"),
    ("outside_time", "invalid_analysis_result"), ("outside_evidence", "invalid_analysis_result"),
])
def test_invalid_observations_fail_instead_of_becoming_review_results(review_settings, synthetic_mov, outcome, code):
    with TestClient(create_app(review_settings, analyzer=ReviewAnalyzer(outcome))) as client:
        accepted = _upload(client, synthetic_mov)
        assert accepted.status_code == 202
        result = _wait(client, accepted.json()["job_id"])
        assert result["status"] == "failed"
        assert result["error"]["code"] == code
        assert not {"verdict", "violations", "observations"}.intersection(result)


def test_review_preserves_authentication_and_upload_limits(review_settings, synthetic_mov):
    settings = replace(review_settings, max_upload_bytes=32)
    with TestClient(create_app(settings, analyzer=ReviewAnalyzer())) as client:
        assert _upload(client, synthetic_mov, headers={}).status_code == 401
        assert _upload(client, synthetic_mov, headers={"X-API-Key": "wrong"}).status_code == 401
        assert _upload(client, synthetic_mov).status_code == 413
        assert client.get("/api/v1/jobs", headers=HEADERS).json()["total"] == 0
        assert client.get("/api/v1/system", headers=HEADERS).json()["busy"] is False


def test_review_and_strict_analysis_share_one_processing_slot(review_settings, synthetic_mov):
    # Enable only this test's fictional catalog to exercise competing routes.
    # Deployment catalogs are never modified by this test.
    catalog = json.loads(review_settings.rule_catalog_path.read_text(encoding="utf-8"))
    for item in catalog["rules"]:
        item.update(enabled=True, verification="verified", verified_on="2026-01-01")
    review_settings.rule_catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    with TestClient(create_app(review_settings, analyzer=ReviewAnalyzer("sleep"))) as client:
        accepted = _upload(client, synthetic_mov)
        assert accepted.status_code == 202
        for route in ("review", "analyze"):
            competing = _upload(client, synthetic_mov, route=route)
            assert competing.status_code == 429
            assert competing.headers["Retry-After"] == "5"
        assert client.get("/api/v1/jobs", headers=HEADERS).json()["total"] == 1


def test_unavailable_runtime_blocks_review_before_saving(review_settings, synthetic_mov, monkeypatch):
    monkeypatch.setattr("app.job_service.analyzer_runtime_status", lambda _: {
        "ready": False, "unavailable_reason": {"code": "model_unavailable", "message": "Synthetic stopped runtime"},
    })
    with TestClient(create_app(review_settings, analyzer=ReviewAnalyzer())) as client:
        status = client.get("/api/v1/system", headers=HEADERS).json()
        assert status["review"]["ready"] is False
        result = _upload(client, synthetic_mov)
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "model_unavailable"
        assert client.get("/api/v1/jobs", headers=HEADERS).json()["total"] == 0


def test_openapi_documents_review_without_verified_verdict(review_settings):
    with TestClient(create_app(review_settings, analyzer=ReviewAnalyzer())) as client:
        specification = client.get("/openapi.json").json()
    route = specification["paths"]["/api/v1/review"]["post"]
    assert route["security"] == [{"ApiKeyAuth": []}]
    assert {"202", "401", "408", "413", "429", "503"}.issubset(route["responses"])
    components = specification["components"]["schemas"]
    review = components["ReviewJobResponse"]["properties"]
    assert {"observations", "review_required", "mode", "analysis_scope"}.issubset(review)
    assert "verdict" not in review and "violations" not in review
    assert "review" in components["SystemStatusResponse"]["properties"]


def test_review_configuration_requires_explicit_opt_in(tmp_path, monkeypatch):
    assert Settings(api_key="synthetic", data_dir=tmp_path).experimental_review is False
    monkeypatch.delenv("APP_EXPERIMENTAL_REVIEW", raising=False)
    assert Settings.from_env().experimental_review is False
    monkeypatch.setenv("APP_EXPERIMENTAL_REVIEW", "true")
    assert Settings.from_env().experimental_review is True
    monkeypatch.setenv("APP_EXPERIMENTAL_REVIEW", "false")
    assert Settings.from_env().experimental_review is False
