from __future__ import annotations

import json
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.analyzer import LocalAnalysisResult
from app.config import Settings
from app.main import create_app
from app.storage import JobStore
from app.video import VideoInfo, VideoValidationError


API_KEY = "local-test-key"
HEADERS = {"X-API-Key": API_KEY}
VIDEO_BYTES = b"test video bytes; validation is explicitly injected in API tests"


class CheckedAnalyzer:
    def analyze(self, context):
        assert context.video_path.is_file()
        assert context.duration_sec == 10.0
        return LocalAnalysisResult(
            verdict="no_violation", violations=[], analysis_complete=True,
            checked_rule_ids=list(context.enabled_rule_ids),
        )


class SleepingAnalyzer:
    def analyze(self, context):
        time.sleep(30)
        return CheckedAnalyzer().analyze(context)


class IncompleteAnalyzer:
    def analyze(self, context):
        return LocalAnalysisResult(verdict="no_violation", violations=[])


class DescendantAnalyzer:
    def analyze(self, context):
        marker = context.video_path.parent / "descendant-survived.txt"
        child_code = "import time,pathlib;time.sleep(4);pathlib.Path(" + repr(str(marker)) + ").write_text('survived')"
        child = subprocess.Popen([sys.executable, "-c", child_code])
        (context.video_path.parent / "child.pid").write_text(str(child.pid))
        time.sleep(30)


class CompletedDescendantAnalyzer:
    def analyze(self, context):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        (context.video_path.parent / "child.pid").write_text(str(child.pid))
        return CheckedAnalyzer().analyze(context)


def process_is_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        try:
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux keeps an exited orphan as a zombie briefly while init reaps it.
    stat = Path(f"/proc/{pid}/stat")
    return not stat.exists() or stat.read_text().split(")", 1)[1].strip().split()[0] != "Z"


def successful_probe(*args):
    return VideoInfo(duration_sec=10.0, width=464, height=848, codec="h264")


@pytest.fixture
def ready_settings(tmp_path, monkeypatch):
    catalog = tmp_path / "test-rules.json"
    catalog.write_text(json.dumps({"version": "test", "rules": [{
        "id": "TEST-RULE", "event_type": "Test event", "enabled": True,
        "verification": "verified", "verified_on": "2026-10-17",
        "reference": {"title": "Test only", "clause": "1", "source_url": "https://example.com/test"},
        "observable_conditions": ["Visible condition"], "exclusions": ["Obscured"],
        "verification_note": "Synthetic test only",
    }]}), encoding="utf-8")
    monkeypatch.setattr("app.main.executable_available", lambda _: True)
    return Settings(api_key=API_KEY, data_dir=tmp_path / "data", max_upload_bytes=1024, rule_catalog_path=catalog)


def upload(client):
    return client.post("/api/v1/analyze", headers=HEADERS, files={"file": ("sample.mov", VIDEO_BYTES, "video/quicktime")})


def wait_result(client, job_id, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/jobs/{job_id}", headers=HEADERS).json()
        if result["status"] != "processing":
            return result
        time.sleep(0.05)
    raise AssertionError("Job did not reach a terminal state")


def test_missing_analyzer_reports_readiness_and_rejects_before_saving(tmp_path):
    settings = Settings(api_key=API_KEY, data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/system").status_code == 401
        assert upload(client).status_code == 503
        status = client.get("/api/v1/system", headers=HEADERS).json()
        assert status["ready"] is False
        assert status["analyzer"]["configured"] is False
        assert status["unavailable_reason"]["code"] == "analyzer_unavailable"
        assert list((tmp_path / "jobs").iterdir()) == []


def test_openapi_declares_api_key_and_complete_job_schemas(tmp_path):
    settings = Settings(api_key=API_KEY, data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        specification = client.get("/openapi.json").json()
        assert specification["components"]["securitySchemes"]["ApiKeyAuth"] == {
            "type": "apiKey", "description": "Use the jury or local server API key. All /api/v1 endpoints require this header.",
            "in": "header", "name": "X-API-Key",
        }
        for path, method in (("/api/v1/system", "get"), ("/api/v1/rules", "get"), ("/api/v1/jobs", "get"), ("/api/v1/analyze", "post"), ("/api/v1/jobs/{job_id}", "get"), ("/api/v1/jobs/{job_id}/video", "get")):
            assert specification["paths"][path][method]["security"] == [{"ApiKeyAuth": []}]
        accepted = specification["paths"]["/api/v1/analyze"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]
        assert accepted["$ref"].endswith("/ProcessingJobResponse")
        job = specification["paths"]["/api/v1/jobs/{job_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        strict_job = next(branch for branch in job["anyOf"] if "discriminator" in branch)
        assert strict_job["discriminator"]["propertyName"] == "status"
        assert set(strict_job["discriminator"]["mapping"]) == {"processing", "done", "failed"}
        assert any(branch.get("$ref", "").endswith("/ReviewJobResponse") for branch in job["anyOf"])
        components = specification["components"]["schemas"]
        system = specification["paths"]["/api/v1/system"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert system["$ref"].endswith("/SystemStatusResponse")
        system_schema = components["SystemStatusResponse"]
        assert system_schema["type"] == "object"
        assert set(system_schema["properties"]) == {"ready", "analyzer", "tools", "limits", "busy", "unavailable_reason", "review"}
        assert system_schema["properties"]["ready"]["type"] == "boolean"
        assert components["MediaToolAvailability"]["properties"]["ffprobe"]["type"] == "boolean"
        assert components["SystemLimits"]["properties"]["max_upload_bytes"]["type"] == "integer"
        assert "metadata" in components["ProcessingJobResponse"]["properties"]
        assert "metadata" in components["FailedJobResponse"]["properties"]
        assert "analysis_scope" in components["DoneJobResponse"]["properties"]
        assert "evidence_frames" in components["Violation"]["properties"]


def test_api_docs_use_only_local_assets_and_disable_external_validator(tmp_path):
    settings = Settings(api_key=API_KEY, data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/docs")
        assert response.status_code == 200
        assert 'src="/static/vendor/swagger-ui/swagger-ui-bundle.js"' in response.text
        assert 'href="/static/vendor/swagger-ui/swagger-ui.css"' in response.text
        assert 'href="/static/favicon.svg"' in response.text
        assert "url: '/openapi.json'" in response.text
        assert '"validatorUrl": null' in response.text
        assert '"persistAuthorization": false' in response.text
        assert "https://" not in response.text
        assert "http://" not in response.text
        assert client.get("/redoc").status_code == 404
        assert client.get("/static/vendor/swagger-ui/swagger-ui-bundle.js").status_code == 200
        assert client.get("/static/vendor/swagger-ui/swagger-ui.css").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200


def test_upload_auth_limits_metadata_and_source_ranges(ready_settings):
    with TestClient(create_app(ready_settings, analyzer=CheckedAnalyzer(), probe=successful_probe)) as client:
        unauthorized = client.post("/api/v1/analyze", files={"file": ("sample.mov", VIDEO_BYTES)})
        assert unauthorized.status_code == 401
        assert client.get("/api/v1/system", headers=HEADERS).json()["ready"] is True
        created = upload(client)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        result = wait_result(client, job_id)
        assert result["status"] == "done"
        assert result["verdict"] == "no_violation"
        assert result["violations"] == []
        assert result["metadata"]["duration_sec"] == 10
        assert result["metadata"]["filename"] == "sample.mov"
        assert result["metadata"]["size_bytes"] == len(VIDEO_BYTES)
        assert client.get(f"/api/v1/jobs/{job_id}/video").status_code == 401
        source = client.get(f"/api/v1/jobs/{job_id}/video", headers=HEADERS)
        assert source.content == VIDEO_BYTES
        assert source.headers["cache-control"] == "no-store"
        partial = client.get(f"/api/v1/jobs/{job_id}/video", headers={**HEADERS, "Range": "bytes=0-3"})
        assert partial.status_code == 206
        assert partial.content == VIDEO_BYTES[:4]
        assert "attachment" in client.get(f"/api/v1/jobs/{job_id}/video?download=true", headers=HEADERS).headers["content-disposition"]


def test_rejects_wrong_extension_oversize_empty_and_invalid_media(ready_settings):
    def invalid_probe(*args):
        raise VideoValidationError("invalid_media", "The file is not a readable video")

    with TestClient(create_app(ready_settings, analyzer=CheckedAnalyzer(), probe=invalid_probe)) as client:
        wrong = client.post("/api/v1/analyze", headers=HEADERS, files={"file": ("sample.mp4", VIDEO_BYTES)})
        assert wrong.status_code == 415
        assert wrong.json()["detail"]["code"] == "unsupported_file_type"
        large = client.post("/api/v1/analyze", headers=HEADERS, files={"file": ("sample.mov", b"x" * 1025)})
        assert large.status_code == 413
        empty = client.post("/api/v1/analyze", headers=HEADERS, files={"file": ("sample.mov", b"")})
        assert empty.status_code == 422
        assert empty.json()["detail"]["code"] == "empty_video"
        invalid = upload(client)
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "invalid_media"
        assert list((ready_settings.data_dir / "jobs").iterdir()) == []
        assert client.get("/api/v1/system", headers=HEADERS).json()["busy"] is False
        assert client.get("/api/v1/jobs/not-a-uuid", headers=HEADERS).status_code == 404


def test_stream_body_limit_without_content_length_and_missing_form_release_slot(ready_settings):
    with TestClient(create_app(ready_settings, analyzer=CheckedAnalyzer(), probe=successful_probe)) as client:
        missing = client.post("/api/v1/analyze", headers=HEADERS)
        assert missing.status_code == 422
        too_large = client.post(
            "/api/v1/analyze",
            headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=test"},
            content=iter([b"--test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample.mov\"\r\n\r\n", b"x" * (1024 * 1024 + 1025), b"\r\n--test--\r\n"]),
        )
        assert too_large.status_code == 413
        assert too_large.json()["detail"]["code"] == "upload_too_large"
        assert client.get("/api/v1/system", headers=HEADERS).json()["busy"] is False


def test_slow_upload_total_deadline_returns_408_and_releases_capacity(ready_settings):
    from dataclasses import replace
    from httpx import ASGITransport, AsyncClient

    settings = replace(ready_settings, upload_timeout_sec=0.1)
    app = create_app(settings, analyzer=CheckedAnalyzer(), probe=successful_probe)

    async def run():
        async def body():
            yield b'--test\r\nContent-Disposition: form-data; name="file"; filename="sample.mov"\r\n\r\n'
            for _ in range(5):
                await asyncio.sleep(0.03)
                yield b"x"
            yield b"\r\n--test--\r\n"

        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
                result = await client.post(
                    "/api/v1/analyze", content=body(),
                    headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=test"},
                )
                assert result.status_code == 408
                assert result.json()["detail"]["code"] == "upload_timeout"
                assert (await client.get("/api/v1/system", headers=HEADERS)).json()["busy"] is False
                assert list((settings.data_dir / "jobs").iterdir()) == []
                assert (await client.post("/api/v1/analyze", headers=HEADERS)).status_code == 422

    asyncio.run(run())


def test_disconnected_upload_releases_reserved_slot(ready_settings):
    app = create_app(ready_settings, analyzer=CheckedAnalyzer(), probe=successful_probe)

    async def run():
        messages = iter([
            {"type": "http.request", "body": b'--test\r\nContent-Disposition: form-data; name="file"; filename="sample.mov"\r\n\r\n', "more_body": True},
            {"type": "http.disconnect"},
        ])
        responses = []

        async def receive():
            return next(messages)

        async def send(message):
            responses.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/api/v1/analyze",
            "raw_path": b"/api/v1/analyze", "query_string": b"", "root_path": "",
            "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
            "headers": [(b"x-api-key", API_KEY.encode()), (b"content-type", b"multipart/form-data; boundary=test")],
        }
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
            assert not app.state.runner.busy
            assert list((ready_settings.data_dir / "jobs").iterdir()) == []
            assert next(message["status"] for message in responses if message["type"] == "http.response.start") == 400

    asyncio.run(run())


def test_busy_returns_429_and_shutdown_recovers_job(ready_settings):
    with TestClient(create_app(ready_settings, analyzer=SleepingAnalyzer(), probe=successful_probe)) as client:
        first = upload(client)
        job_id = first.json()["job_id"]
        second = upload(client)
        assert second.status_code == 429
        assert second.headers["retry-after"] == "5"
        assert second.json()["detail"]["code"] == "server_busy"
        assert client.get("/api/v1/system", headers=HEADERS).json()["busy"] is True
    with TestClient(create_app(ready_settings)) as client:
        interrupted = client.get(f"/api/v1/jobs/{job_id}", headers=HEADERS).json()
        assert interrupted["status"] == "failed"
        assert interrupted["error"]["code"] == "interrupted"


def test_timeout_is_enforced_and_slot_released(ready_settings):
    from dataclasses import replace

    settings = replace(ready_settings, job_timeout_sec=0.5)
    with TestClient(create_app(settings, analyzer=SleepingAnalyzer(), probe=successful_probe)) as client:
        job_id = upload(client).json()["job_id"]
        result = wait_result(client, job_id)
        assert result["status"] == "failed"
        assert result["error"]["code"] == "analysis_timeout"
        deadline = time.monotonic() + 10
        while client.get("/api/v1/system", headers=HEADERS).json()["busy"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert len(list((settings.data_dir / "jobs").iterdir())) == 1


def test_timeout_terminates_analyzers_child_process(ready_settings):
    from dataclasses import replace

    settings = replace(ready_settings, job_timeout_sec=3)
    with TestClient(create_app(settings, analyzer=DescendantAnalyzer(), probe=successful_probe)) as client:
        job_id = upload(client).json()["job_id"]
        directory = settings.data_dir / "jobs" / job_id
        result = wait_result(client, job_id)
        assert result["error"]["code"] == "analysis_timeout"
        assert (directory / "child.pid").is_file(), "The test must actually launch a descendant before timing out"
        deadline = time.monotonic() + 10
        while client.get("/api/v1/system", headers=HEADERS).json()["busy"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        child_pid = int((directory / "child.pid").read_text())
        assert not process_is_running(child_pid), "Analyzer descendant is still running after the timeout"
        assert not (directory / "descendant-survived.txt").exists()


def test_completed_analyzer_does_not_leave_helper_process_running(ready_settings):
    with TestClient(create_app(ready_settings, analyzer=CompletedDescendantAnalyzer(), probe=successful_probe)) as client:
        job_id = upload(client).json()["job_id"]
        result = wait_result(client, job_id)
        assert result["status"] == "done"
        deadline = time.monotonic() + 10
        while client.get("/api/v1/system", headers=HEADERS).json()["busy"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        pid = int((ready_settings.data_dir / "jobs" / job_id / "child.pid").read_text())
        assert not process_is_running(pid), "A completed worker left a helper process running"


def test_incomplete_analysis_cannot_become_no_violation(ready_settings):
    with TestClient(create_app(ready_settings, analyzer=IncompleteAnalyzer(), probe=successful_probe)) as client:
        result = wait_result(client, upload(client).json()["job_id"])
        assert result["status"] == "failed"
        assert result["error"]["code"] == "insufficient_evidence"
        assert "verdict" not in result


def test_restart_marks_preexisting_processing_job_failed(tmp_path):
    store = JobStore(tmp_path)
    store.initialize()
    job_id = str(uuid4())
    store.create(job_id, "interrupted.mov", {"duration_sec": 10})
    settings = Settings(api_key=API_KEY, data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        result = client.get(f"/api/v1/jobs/{job_id}", headers=HEADERS).json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == "interrupted"
        assert result["metadata"]["filename"] == "interrupted.mov"


def test_second_api_process_cannot_take_over_active_data_directory(tmp_path):
    settings = Settings(api_key=API_KEY, data_dir=tmp_path)
    with TestClient(create_app(settings)) as first:
        with pytest.raises(RuntimeError, match="already used"):
            with TestClient(create_app(settings)):
                pass
        assert first.get("/api/v1/system", headers=HEADERS).status_code == 200
    # Releasing the server lock permits a normal subsequent launch.
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/api/v1/system", headers=HEADERS).status_code == 200


@pytest.mark.parametrize("failure_at", ["directory", "file", "database"])
def test_storage_failures_return_507_and_leave_no_upload_or_busy_slot(ready_settings, monkeypatch, failure_at):
    app = create_app(ready_settings, analyzer=CheckedAnalyzer(), probe=successful_probe)
    with TestClient(app) as client:
        if failure_at == "database":
            import sqlite3

            with sqlite3.connect(app.state.store.db_path) as database:
                database.execute("""CREATE TRIGGER unavailable_storage BEFORE INSERT ON jobs
                    BEGIN SELECT RAISE(FAIL, 'Synthetic database write failure'); END""")
        original_mkdir, original_open = Path.mkdir, Path.open

        def mkdir(path, *args, **kwargs):
            if failure_at == "directory" and path.parent == app.state.store.jobs_dir:
                raise PermissionError("Synthetic unavailable disk")
            return original_mkdir(path, *args, **kwargs)

        def open_file(path, *args, **kwargs):
            if failure_at == "file" and path.name == "input.mov":
                raise OSError("Synthetic full disk")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", mkdir)
        monkeypatch.setattr(Path, "open", open_file)
        response = upload(client)
        assert response.status_code == 507
        assert response.json()["detail"] == {
            "code": "storage_unavailable", "message": "The video could not be stored on the local server",
        }
        assert client.get("/api/v1/system", headers=HEADERS).json()["busy"] is False
        assert client.get("/api/v1/jobs", headers=HEADERS).json()["total"] == 0
        assert list(app.state.store.jobs_dir.iterdir()) == []
        assert client.post("/api/v1/analyze", headers=HEADERS).status_code == 422


@pytest.mark.parametrize("cancellation_count", [0, 1, 2])
def test_slow_local_file_write_keeps_other_http_requests_responsive(ready_settings, monkeypatch, cancellation_count):
    import threading
    from httpx import ASGITransport, AsyncClient

    app = create_app(ready_settings, analyzer=CheckedAnalyzer(), probe=successful_probe)
    writing, release_write = threading.Event(), threading.Event()
    original_open = Path.open

    class SlowTarget:
        def __init__(self, target):
            self.target = target

        def __enter__(self):
            self.target.__enter__()
            return self

        def __exit__(self, *args):
            return self.target.__exit__(*args)

        def write(self, chunk):
            writing.set()
            assert release_write.wait(timeout=5), "Concurrent request did not finish while the write was blocked"
            return self.target.write(chunk)

    def open_file(path, *args, **kwargs):
        target = original_open(path, *args, **kwargs)
        return SlowTarget(target) if path.name == "input.mov" and args == ("xb",) else target

    monkeypatch.setattr(Path, "open", open_file)

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
                posting = asyncio.create_task(client.post(
                    "/api/v1/analyze", headers=HEADERS, files={"file": ("sample.mov", VIDEO_BYTES)},
                ))
                try:
                    assert await asyncio.to_thread(writing.wait, 3)
                    for _ in range(cancellation_count):
                        posting.cancel()
                        await asyncio.sleep(0)
                        status = await asyncio.wait_for(client.get("/api/v1/system", headers=HEADERS), timeout=1)
                        assert status.json()["busy"] is True
                        competing = await client.post("/api/v1/analyze", headers=HEADERS)
                        assert competing.status_code == 429
                        assert not posting.done()
                    status = await asyncio.wait_for(client.get("/api/v1/system", headers=HEADERS), timeout=1)
                    assert status.status_code == 200
                    assert status.json()["busy"] is True
                    assert not posting.done()
                finally:
                    release_write.set()
                if cancellation_count:
                    with pytest.raises(asyncio.CancelledError):
                        await posting
                    # A completely received video finishes admission even if its
                    # HTTP caller leaves; it remains discoverable in history.
                    history = (await client.get("/api/v1/jobs", headers=HEADERS)).json()
                    assert history["total"] == 1
                    job_id = history["items"][0]["job_id"]
                else:
                    accepted = await posting
                    assert accepted.status_code == 202
                    job_id = accepted.json()["job_id"]
                # A persisted processing row alone is insufficient: the runner
                # must have received the job and reached its observable result.
                deadline = time.monotonic() + 12
                while True:
                    result = (await client.get(f"/api/v1/jobs/{job_id}", headers=HEADERS)).json()
                    if result["status"] != "processing":
                        break
                    assert time.monotonic() < deadline, "The saved video was never handed to the runner"
                    await asyncio.sleep(0.05)
                assert result["status"] == "done"
                assert result["verdict"] == "no_violation"
                assert (await client.get(f"/api/v1/jobs/{job_id}/video", headers=HEADERS)).content == VIDEO_BYTES

    asyncio.run(run())


def test_rejected_api_requests_are_not_cacheable(tmp_path):
    with TestClient(create_app(Settings(api_key=API_KEY, data_dir=tmp_path))) as client:
        response = client.get("/api/v1/jobs")
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_supervisor_start_failure_is_terminal_and_releases_analysis_slot(ready_settings, monkeypatch):
    """An accepted upload must not remain processing if its watcher cannot start."""
    import app.worker as worker

    real_thread = worker.threading.Thread
    failed_once = False
    unwatched_workers = []

    class UnstartableSupervisor:
        def start(self):
            raise RuntimeError("Synthetic operating system thread limit")

        def join(self, timeout=None):
            pass

    def create_thread(*args, **kwargs):
        nonlocal failed_once
        if getattr(kwargs.get("target"), "__name__", None) == "_supervise" and not failed_once:
            failed_once = True
            unwatched_workers.append(kwargs["args"][1])
            return UnstartableSupervisor()
        return real_thread(*args, **kwargs)

    app = create_app(ready_settings, analyzer=SleepingAnalyzer(), probe=successful_probe)
    with TestClient(app) as client:
        monkeypatch.setattr(worker.threading, "Thread", create_thread)
        accepted = upload(client)
        assert accepted.status_code == 202
        result = client.get(f"/api/v1/jobs/{accepted.json()['job_id']}", headers=HEADERS).json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == "worker_start_failed"
        assert client.get("/api/v1/system", headers=HEADERS).json()["busy"] is False
        assert unwatched_workers[0].pid is not None
        assert not process_is_running(unwatched_workers[0].pid), "An unsupervised analyzer survived its startup failure"
        # A later request is admitted after the operating system recovers.
        retry = upload(client)
        assert retry.status_code == 202


def test_upload_finishing_after_shutdown_does_not_launch_a_new_worker(ready_settings):
    import threading
    from httpx import ASGITransport, AsyncClient

    probing, finish_probe = threading.Event(), threading.Event()

    def delayed_probe(*args):
        probing.set()
        assert finish_probe.wait(timeout=5), "The shutdown did not finish while validation was pending"
        return successful_probe(*args)

    app = create_app(ready_settings, analyzer=SleepingAnalyzer(), probe=delayed_probe)

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
                posting = asyncio.create_task(client.post(
                    "/api/v1/analyze", headers=HEADERS, files={"file": ("sample.mov", VIDEO_BYTES)},
                ))
                try:
                    assert await asyncio.to_thread(probing.wait, 3)
                    await asyncio.to_thread(app.state.runner.shutdown)
                finally:
                    finish_probe.set()
                accepted = await posting
                assert accepted.status_code == 202
                result = (await client.get(f"/api/v1/jobs/{accepted.json()['job_id']}", headers=HEADERS)).json()
                assert result["status"] == "failed"
                assert result["error"]["code"] == "interrupted"
                assert (await client.get("/api/v1/system", headers=HEADERS)).json()["busy"] is False

    asyncio.run(run())
