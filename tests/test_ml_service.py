"""Exercise the installed ML adapter through HTTP with local synthetic fixtures."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
from io import BytesIO
import json
import os
import shutil
import subprocess
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.config import Settings
from app.main import create_app
from app.ml.rules import RULES


KEY = "synthetic-ml-service-test"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture
def model_server(monkeypatch):
    state = {"healthy": True, "health_calls": 0, "chat_calls": 0, "mode": "positive"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/health"
            state["health_calls"] += 1
            self.send_response(200 if state["healthy"] else 503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            images = [item for item in payload["messages"][1]["content"] if item["type"] == "image_url"]
            assert len(images) >= 3
            for item in images:
                assert item["image_url"]["url"].startswith("data:image/jpeg;base64,")
            state["chat_calls"] += 1
            window = {"assessable": state["mode"] != "unassessable", "events": []}
            if state["mode"] == "positive":
                window["events"] = [{"rule_id": "R22", "start_frame": 0, "peak_frame": 1, "end_frame": 2,
                                     "conditions": list(RULES["R22"].criteria),
                                     "observation": "Синтетический ответ для проверки стыковки; банковские действия не распознаются."}]
            body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(window)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    monkeypatch.setenv("APP_LOCAL_MODEL_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def ml_settings(tmp_path, synthetic_ml_catalog, monkeypatch):
    catalog = tmp_path / "synthetic-rules.json"
    catalog.write_text(synthetic_ml_catalog.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("app.main.executable_available", lambda _: True)
    return Settings(api_key=KEY, data_dir=tmp_path / "jobs-data", rule_catalog_path=catalog,
                    analyzer_factory="app.local_analyzer:build_analyzer")


@pytest.fixture
def actual_media(ml_settings, tmp_path):
    ffmpeg = shutil.which(os.getenv("APP_FFMPEG_BIN", "ffmpeg"))
    ffprobe = shutil.which(os.getenv("APP_FFPROBE_BIN", "ffprobe"))
    if not ffmpeg or not ffprobe:
        pytest.skip("Actual ML/service transport check requires FFmpeg and FFprobe")
    video = tmp_path / "generated.mov"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
                    "-i", "testsrc2=size=320x240:rate=10", "-t", "2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-an", str(video)], check=True, capture_output=True, timeout=20)
    return replace(ml_settings, ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe), video


def test_model_failure_removes_readiness_and_rejects_upload_before_creating_a_job(ml_settings, model_server):
    with TestClient(create_app(ml_settings)) as client:
        assert client.get("/api/v1/system").status_code == 401
        assert model_server["health_calls"] == 0
        assert client.get("/api/v1/system", headers=HEADERS).json()["ready"] is True
        model_server["healthy"] = False
        status = client.get("/api/v1/system", headers=HEADERS).json()
        assert status["ready"] is False
        assert status["unavailable_reason"]["code"] == "local_model_unavailable"
        response = client.post("/api/v1/analyze", headers=HEADERS,
                               files={"file": ("synthetic.mov", b"must not be accepted")})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "local_model_unavailable"
        assert client.get("/api/v1/jobs", headers=HEADERS).json()["total"] == 0
        assert not list((ml_settings.data_dir / "jobs").iterdir())


def test_real_adapter_decodes_mov_calls_loopback_and_service_saves_evidence(actual_media, model_server):
    settings, video = actual_media
    with TestClient(create_app(settings)) as client:
        accepted = client.post("/api/v1/analyze", headers=HEADERS,
                               files={"file": ("generated.mov", video.read_bytes(), "video/quicktime")})
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = client.get("/api/v1/jobs/" + job_id, headers=HEADERS).json()
            if result["status"] != "processing":
                break
            time.sleep(.05)
        assert result["status"] == "done", result
        assert result["verdict"] == "violation"
        assert result["analysis_scope"]["full_video_checked"] is True
        assert result["analysis_scope"]["checked_rule_ids"] == list(RULES)
        event = result["violations"][0]
        assert event["timecode_sec"] == .5
        assert event["rule"]["source_url"] == "https://example.com/synthetic"
        assert [frame["timestamp_sec"] for frame in event["evidence_frames"]] == [0, .5, 1]
        for frame in event["evidence_frames"]:
            with Image.open(BytesIO(base64.b64decode(frame["image_base64"], validate=True))) as image:
                assert image.format == "JPEG"
                image.load()
        assert client.get("/api/v1/jobs", headers=HEADERS).json()["total"] == 1
        assert model_server["chat_calls"] == 2


@pytest.mark.parametrize("mode, expected_status", [("negative", "done"), ("unassessable", "failed")])
def test_real_adapter_distinguishes_complete_negative_from_missing_context(actual_media, model_server, mode, expected_status):
    settings, video = actual_media
    model_server["mode"] = mode
    with TestClient(create_app(settings)) as client:
        accepted = client.post("/api/v1/analyze", headers=HEADERS,
                               files={"file": ("generated.mov", video.read_bytes(), "video/quicktime")})
        assert accepted.status_code == 202
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = client.get("/api/v1/jobs/" + accepted.json()["job_id"], headers=HEADERS).json()
            if result["status"] != "processing":
                break
            time.sleep(.05)
        assert result["status"] == expected_status, result
        if mode == "negative":
            assert result["verdict"] == "no_violation" and result["violations"] == []
            assert result["analysis_scope"]["full_video_checked"] is True
        else:
            assert "verdict" not in result
            assert result["error"]["code"] == "insufficient_evidence"
        assert model_server["chat_calls"] == 1
