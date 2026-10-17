"""Admission readiness for the optional local model runtime, without inference."""
from __future__ import annotations

import importlib
import os
import socket
import time
from urllib.parse import urlsplit


_LOCAL_FACTORY = "app.local_analyzer:build_analyzer"
_PROBE_SECONDS = 0.75
_MAX_HEADERS = 16 * 1024


def analyzer_runtime_status(factory_spec: str | None) -> dict | None:
    """Check the known adapter's decoder and loopback HTTP health endpoint only.

    Other factories retain their existing readiness contract and perform no
    optional imports or I/O here. This never initializes an analyzer, loads
    weights, downloads files or sends video. HTTP 200 confirms availability;
    it does not prove the runtime's model identity or recognition quality.
    """
    if factory_spec != _LOCAL_FACTORY:
        return None
    try:
        importlib.import_module("av")
        from app.ml.model import validate_endpoint
    except (ImportError, OSError):
        return _unavailable("ml_dependencies_unavailable", "The local analyzer requires its installed ML dependencies")
    try:
        endpoint = validate_endpoint(os.getenv("APP_LOCAL_MODEL_URL", "http://127.0.0.1:8091"))
    except (TypeError, ValueError):
        return _unavailable("analyzer_config_invalid", "The local model endpoint must be a literal loopback HTTP address")
    if not _healthy(endpoint):
        return _unavailable("local_model_unavailable", "The local model is stopped, loading or unavailable")
    return {"ready": True, "unavailable_reason": None}


def _unavailable(code: str, message: str) -> dict:
    return {"ready": False, "unavailable_reason": {"code": code, "message": message}}


def _healthy(endpoint: str) -> bool:
    """Bound connect plus the complete response headers by one wall deadline.

    Direct literal-address sockets cannot use proxies or follow redirects.
    Reading only capped headers avoids arbitrary bodies and repeated socket
    timeouts extending the deadline when a server sends headers slowly.
    """
    address = urlsplit(endpoint)
    deadline = time.monotonic() + _PROBE_SECONDS
    family = socket.AF_INET6 if address.hostname == "::1" else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.connect((address.hostname, address.port))
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sendall((f"GET /health HTTP/1.1\r\nHost: {address.netloc}\r\nConnection: close\r\n\r\n").encode("ascii"))
            headers = bytearray()
            while b"\r\n\r\n" not in headers:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(headers) >= _MAX_HEADERS:
                    return False
                connection.settimeout(remaining)
                chunk = connection.recv(min(4096, _MAX_HEADERS - len(headers)))
                if not chunk:
                    return False
                headers.extend(chunk)
            first_line = bytes(headers).split(b"\r\n", 1)[0].split(b" ", 2)
            return len(first_line) >= 2 and first_line[0] in {b"HTTP/1.0", b"HTTP/1.1"} and first_line[1] == b"200"
    except (OSError, ValueError, UnicodeError):
        return False
