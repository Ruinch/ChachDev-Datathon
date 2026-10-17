from __future__ import annotations

from fastapi.testclient import TestClient

from app.analyzer import LocalAnalysisResult
from app.config import Settings
from app.main import create_app


API_KEY = "local-test-key"
VIDEO_BYTES = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00"


def test_upload_needs_api_key_and_unconfigured_analysis_fails(tmp_path):
    settings = Settings(api_key=API_KEY, data_dir=tmp_path, max_upload_bytes=1024)
    with TestClient(create_app(settings)) as client:
        unauthenticated = client.post(
            "/api/v1/analyze", files={"file": ("sample.mov", VIDEO_BYTES)}
        )
        assert unauthenticated.status_code == 401

        created = client.post(
            "/api/v1/analyze",
            headers={"X-API-Key": API_KEY},
            files={"file": ("sample.mov", VIDEO_BYTES, "video/quicktime")},
        )
        assert created.status_code == 202
        assert created.json()["status"] == "processing"
        job_id = created.json()["job_id"]

        assert (tmp_path / "jobs" / job_id / "input.mov").read_bytes() == VIDEO_BYTES
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 401
        result = client.get(
            f"/api/v1/jobs/{job_id}", headers={"X-API-Key": API_KEY}
        )
        assert result.status_code == 200
        assert result.json() == {
            "job_id": job_id,
            "status": "failed",
            "error": {
                "code": "analyzer_unavailable",
                "message": "A local video analyzer has not been configured for this server",
            },
        }


def test_rejects_wrong_extension_and_oversized_upload(tmp_path):
    settings = Settings(api_key=API_KEY, data_dir=tmp_path, max_upload_bytes=8)
    with TestClient(create_app(settings)) as client:
        headers = {"X-API-Key": API_KEY}
        wrong_extension = client.post(
            "/api/v1/analyze",
            headers=headers,
            files={"file": ("sample.mp4", VIDEO_BYTES)},
        )
        assert wrong_extension.status_code == 415

        too_large = client.post(
            "/api/v1/analyze",
            headers=headers,
            files={"file": ("sample.mov", VIDEO_BYTES)},
        )
        assert too_large.status_code == 413
        assert list((tmp_path / "jobs").iterdir()) == []

        unknown_job = client.get(
            "/api/v1/jobs/not-a-uuid", headers=headers
        )
        assert unknown_job.status_code == 404


def test_done_shape_comes_from_an_explicit_analyzer(tmp_path):
    class CheckedAnalyzer:
        def analyze(self, context):
            assert context.video_path.is_file()
            return LocalAnalysisResult(verdict="no_violation", violations=[])

    settings = Settings(api_key=API_KEY, data_dir=tmp_path, max_upload_bytes=1024)
    with TestClient(create_app(settings, analyzer=CheckedAnalyzer())) as client:
        created = client.post(
            "/api/v1/analyze",
            headers={"X-API-Key": API_KEY},
            files={"file": ("sample.mov", VIDEO_BYTES)},
        )
        job_id = created.json()["job_id"]
        result = client.get(
            f"/api/v1/jobs/{job_id}", headers={"X-API-Key": API_KEY}
        )
        assert result.json() == {
            "job_id": job_id,
            "status": "done",
            "verdict": "no_violation",
            "violations": [],
        }
