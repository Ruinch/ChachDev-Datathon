"""Acceptance client exercised against the actual API over loopback TCP."""
from contextlib import contextmanager
import socket
import threading
import time

import uvicorn

from app.main import create_app
from test_pipeline_integration import (KEY, SyntheticEventsAnalyzer, media_settings,
                                      media_tools, synthetic_video)


@contextmanager
def running_api(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+10
        while not server.started and time.monotonic()<deadline:
            time.sleep(.02)
        assert server.started
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_probe_checks_multi_event_api_without_saving_evidence(media_settings, synthetic_video):
    from ml_tools.submission_probe import probe
    with running_api(create_app(media_settings, analyzer=SyntheticEventsAnalyzer())) as url:
        result = probe(url, synthetic_video, KEY, verify_source=True)
    assert result['status'] == 'done'
    assert result['verdict'] == 'violation'
    assert result['events'] == 2 and result['valid_jpeg_frames'] == 4
    assert result['catalog_references_match'] and result['source_sha256_match']
    assert result['range_matches_source'] and result['result_reopened'] and result['listed_in_history']
    assert result['unauthenticated_status'] == 401 and result['within_300_seconds']
    assert KEY not in str(result) and 'image_base64' not in str(result)
