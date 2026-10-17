"""Runtime admission checks use only an artificial loopback HTTP service."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import sys
import threading
import time

import pytest


FACTORY = "app.local_analyzer:build_analyzer"


@contextmanager
def runtime(status=200, *, redirect=None, pause=0, trickle=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if pause:
                time.sleep(pause)
            try:
                if trickle:
                    for chunk in (b"HTTP/1.1 200 OK\r\n", b"X-Test: slow", b" response", b"\r\n\r\n"):
                        self.connection.sendall(chunk)
                        time.sleep(0.35)
                    return
                self.send_response(status)
                if redirect:
                    self.send_header("Location", redirect)
                self.end_headers()
            except OSError:
                pass  # The deadline test deliberately closes its client first.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def status():
    from app.analyzer_readiness import analyzer_runtime_status
    return analyzer_runtime_status(FACTORY)


@pytest.fixture
def decoder_dependency():
    return pytest.importorskip("av", reason="Install .[ml] to test healthy ML runtime admission")


def test_healthy_runtime_receives_only_health_probe_and_ignores_proxy(monkeypatch, decoder_dependency):
    with runtime() as (endpoint, requests):
        monkeypatch.setenv("APP_LOCAL_MODEL_URL", endpoint)
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
        assert status() == {"ready": True, "unavailable_reason": None}
    assert len(requests) == 1 and requests[0][0] == "/health"
    assert "X-API-Key" not in requests[0][1]


@pytest.mark.parametrize("http_status", [302, 503])
def test_loading_and_redirects_are_unavailable_without_followup(monkeypatch, http_status, decoder_dependency):
    with runtime() as (destination, destination_requests):
        with runtime(http_status, redirect=destination + "/forbidden") as (endpoint, requests):
            monkeypatch.setenv("APP_LOCAL_MODEL_URL", endpoint)
            result = status()
        assert not destination_requests
    assert result["ready"] is False
    assert result["unavailable_reason"]["code"] == "local_model_unavailable"
    assert len(requests) == 1


def test_stopped_runtime_is_unavailable(monkeypatch, decoder_dependency):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    monkeypatch.setenv("APP_LOCAL_MODEL_URL", f"http://127.0.0.1:{port}")
    assert status()["unavailable_reason"]["code"] == "local_model_unavailable"


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1:8091", "http://example.com:8091", "http://localhost:8091", "http://127.0.0.1:8091/redirect"])
def test_invalid_endpoint_is_configuration_error(monkeypatch, endpoint, decoder_dependency):
    monkeypatch.setenv("APP_LOCAL_MODEL_URL", endpoint)
    assert status()["unavailable_reason"]["code"] == "analyzer_config_invalid"


def test_missing_decoder_dependency_is_reported_before_request(monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)
    with runtime() as (endpoint, requests):
        monkeypatch.setenv("APP_LOCAL_MODEL_URL", endpoint)
        assert status()["unavailable_reason"]["code"] == "ml_dependencies_unavailable"
    assert not requests


@pytest.mark.parametrize("factory", [None, "custom.factory:build_analyzer"])
def test_other_factories_have_no_optional_import_or_network(monkeypatch, factory):
    from app.analyzer_readiness import analyzer_runtime_status
    monkeypatch.setitem(sys.modules, "av", None)
    monkeypatch.setenv("APP_LOCAL_MODEL_URL", "https://example.com")

    def forbidden(*args, **kwargs):
        pytest.fail("Generic readiness must perform no network I/O")

    monkeypatch.setattr(socket, "socket", forbidden)
    assert analyzer_runtime_status(factory) is None


@pytest.mark.parametrize("options", [{"pause": 1.5}, {"trickle": True}])
def test_slow_runtime_probe_has_one_overall_deadline(monkeypatch, decoder_dependency, options):
    with runtime(**options) as (endpoint, _):
        monkeypatch.setenv("APP_LOCAL_MODEL_URL", endpoint)
        started = time.monotonic()
        result = status()
        elapsed = time.monotonic() - started
    assert result["unavailable_reason"]["code"] == "local_model_unavailable"
    assert elapsed < 1.25
