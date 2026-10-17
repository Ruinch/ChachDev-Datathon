from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage import JobStore


HEADERS = {"X-API-Key": "history-test-key"}


def settings_for(tmp_path, **overrides):
    return Settings(api_key=HEADERS["X-API-Key"], data_dir=tmp_path, **overrides)


def complete(store, filename, verdict="violation", *, count=2, scope=True):
    job_id = str(uuid4())
    store.create(job_id, filename, {"size_bytes": 123, "duration_sec": 10, "width": 464, "height": 848, "codec": "h264"})
    result = {
        "verdict": verdict,
        "violations": [{"description": "PRIVATE EVENT DETAIL", "evidence_frames": [{"image_base64": "HEAVY_EVIDENCE" * 10000}]}] * count,
    }
    if scope:
        result["analysis_scope"] = {
            "full_video_checked": True, "checked_rule_ids": ["TEST"], "enabled_rule_ids": ["TEST"], "limitations": [],
        }
    store.set_done(job_id, result)
    return job_id


def test_history_auth_pagination_filters_and_global_summary(tmp_path, monkeypatch):
    import app.storage as storage_module

    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        store = app.state.store
        positive = complete(store, "КАССА_%_1.mov")
        negative = complete(store, "clear.mov", "no_violation", count=0)
        failed = str(uuid4())
        store.create(failed, "failure.mov")
        store.set_failed(failed, "test_failure", "A test failure")
        processing = str(uuid4())
        store.create(processing, "running.mov")
        summary = {"total": 4, "processing": 1, "done": 2, "failed": 1, "violation": 1, "no_violation": 1}
        assert client.get("/api/v1/jobs").status_code == 401
        assert client.get("/api/v1/jobs", headers={"X-API-Key": "wrong"}).status_code == 401

        original_loads = storage_module.json.loads

        def no_heavy_results(value, *args, **kwargs):
            assert "HEAVY_EVIDENCE" not in str(value), "History read an image payload"
            assert "PRIVATE EVENT DETAIL" not in str(value), "History read event descriptions"
            return original_loads(value, *args, **kwargs)

        monkeypatch.setattr(storage_module.json, "loads", no_heavy_results)
        page = client.get("/api/v1/jobs?limit=2&offset=1", headers=HEADERS)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        result = page.json()
        assert result["summary"] == summary
        assert result["total"] == 4
        assert result["limit"] == 2 and result["offset"] == 1
        assert [item["job_id"] for item in result["items"]] == [failed, negative]
        assert "error" not in result["items"][0]

        cases = [
            ({"q": "касса_%"}, [positive]),
            ({"q": "%"}, [positive]),
            ({"q": "_"}, [positive]),
            ({"q": "clear"}, [negative]),
            ({"status": "done"}, [negative, positive]),
            ({"status": "processing"}, [processing]),
            ({"status": "failed"}, [failed]),
            ({"verdict": "violation"}, [positive]),
            ({"verdict": "no_violation"}, [negative]),
            ({"status": "processing", "verdict": "violation"}, []),
            ({"q": "%' OR 1=1 --"}, []),
        ]
        for parameters, expected in cases:
            filtered = client.get("/api/v1/jobs", headers=HEADERS, params=parameters).json()
            assert [item["job_id"] for item in filtered["items"]] == expected
            assert filtered["total"] == len(expected)
            assert filtered["summary"] == summary
        item = client.get("/api/v1/jobs", headers=HEADERS, params={"q": "КАССА"}).json()["items"][0]
        assert set(item) == {"job_id", "status", "verdict", "event_count", "metadata", "analysis_scope"}
        assert item["event_count"] == 2
        assert set(item["metadata"]) == {"filename", "size_bytes", "duration_sec", "width", "height", "codec", "created_at", "updated_at"}
        assert item["metadata"]["size_bytes"] == 123
        assert item["analysis_scope"]["full_video_checked"] is True
        assert client.get("/api/v1/jobs?offset=100", headers=HEADERS).json()["items"] == []


@pytest.mark.parametrize("parameters", [
    {"limit": 0}, {"limit": 101}, {"limit": "bad"}, {"offset": -1},
    {"status": "finished"}, {"verdict": "unknown"}, {"q": "x" * 256},
])
def test_history_validates_query_parameters(tmp_path, parameters):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        assert client.get("/api/v1/jobs", headers=HEADERS, params=parameters).status_code == 422


def test_empty_history_has_zero_summary(tmp_path):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        result = client.get("/api/v1/jobs", headers=HEADERS).json()
        assert result == {
            "items": [], "total": 0, "limit": 20, "offset": 0,
            "summary": {"total": 0, "processing": 0, "done": 0, "failed": 0, "violation": 0, "no_violation": 0},
        }


def test_history_migrates_legacy_records_and_survives_restart(tmp_path):
    database = tmp_path / "jobs.sqlite3"
    old_id, corrupt_id = str(uuid4()), str(uuid4())
    with sqlite3.connect(database) as conn:
        conn.execute("""CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY, status TEXT NOT NULL, filename TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT,
            error_code TEXT, error_message TEXT
        )""")
        for job_id, result in (
            (old_id, json.dumps({"verdict": "violation", "violations": [{"evidence_frames": [{"image_base64": "legacy-image"}]}]})),
            (corrupt_id, "not-json"),
        ):
            conn.execute("INSERT INTO jobs VALUES (?, 'done', ?, ?, ?, ?, NULL, NULL)",
                         (job_id, "old.mov", "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z", result))
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings)) as client:
        first = client.get("/api/v1/jobs", headers=HEADERS).json()
        assert first["summary"]["violation"] == 1
        assert first["summary"]["done"] == 2
        items = {item["job_id"]: item for item in first["items"]}
        assert items[old_id]["verdict"] == "violation"
        assert items[old_id]["event_count"] == 1
        assert "analysis_scope" not in items[old_id]
        assert items[old_id]["metadata"]["size_bytes"] is None
        assert items[corrupt_id]["verdict"] is None
        assert items[corrupt_id]["event_count"] == 0
        assert [item["job_id"] for item in first["items"]] == sorted([old_id, corrupt_id], reverse=True)
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/api/v1/jobs", headers=HEADERS).json() == first


def test_rules_exposes_disabled_candidates_without_claiming_activation(tmp_path):
    with TestClient(create_app(settings_for(tmp_path))) as client:
        assert client.get("/api/v1/rules").status_code == 401
        assert client.get("/api/v1/rules", headers={"X-API-Key": "wrong"}).status_code == 401
        response = client.get("/api/v1/rules", headers=HEADERS)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert result["total"] == 2 and result["enabled_count"] == 0
        for item in result["items"]:
            assert item["enabled"] is False
            assert item["verification"] == "candidate"
            assert item["verified_on"] is None
            assert item["observable_signals"] and item["limitations"]
            assert item["required_context"] == []
            assert item["verification_note"]
            assert set(item["reference"]) == {"title", "clause", "source_url"}


def test_invalid_catalog_returns_explicit_service_error(tmp_path):
    with TestClient(create_app(settings_for(tmp_path, rule_catalog_path=tmp_path / "missing.json"))) as client:
        result = client.get("/api/v1/rules", headers=HEADERS)
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "invalid_rule_catalog"

