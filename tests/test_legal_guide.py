from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

import app.legal_guide as legal_module
from app.config import Settings
from app.legal_guide import LegalGuideUnavailableError, load_legal_guide
from app.main import create_app


HEADERS = {"X-API-Key": "legal-guide-test-key"}


@pytest.fixture
def guide_document():
    return {
        "version": "test-reference-1", "reviewed_on": "2026-10-18", "jurisdiction": "KZ",
        "purpose": "Explanatory fixture, not a finding about a video.", "result_binding": "reference_only",
        "sources": [{
            "id": "SOURCE", "title": "Official source fixture",
            "url": "https://adilet.zan.kz/rus/docs/test#p20", "locator": "Paragraph fixture",
            "edition_note": "Synthetic test metadata", "checked_on": "2026-10-18", "verification_status": "checked",
        }],
        "principles": [{"title": "Visibility", "text": "Missing context does not establish a violation."}],
        "procedures": [{
            "id": "PROCEDURE", "title": "Procedure fixture", "related_rule_ids": ["R20-A"],
            "source_ids": ["SOURCE"], "basis": "interpretation", "requirement": "Review the complete operation.",
            "correct": ["Visible compliant procedure."], "potential_deviation": ["Visible contrary action."],
            "required_context": ["Complete operation."], "cannot_conclude": ["Client area is outside the frame."],
            "notes": [],
        }],
    }


def write_guide(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_legal_guide_endpoint_uses_real_loader_auth_and_does_not_activate_rules(tmp_path, monkeypatch, guide_document):
    guide_path = tmp_path / "guide.json"
    write_guide(guide_path, guide_document)
    monkeypatch.setattr(legal_module, "DEFAULT_LEGAL_GUIDE", guide_path)
    settings = Settings(api_key=HEADERS["X-API-Key"], data_dir=tmp_path / "jobs")
    with TestClient(create_app(settings)) as client:
        for headers in ({}, {"X-API-Key": "wrong"}):
            denied = client.get("/api/v1/legal-guide", headers=headers)
            assert denied.status_code == 401
            assert denied.headers["cache-control"] == "no-store"
        status_before = client.get("/api/v1/system", headers=HEADERS).json()
        rules_before = client.get("/api/v1/rules", headers=HEADERS).json()
        response = client.get("/api/v1/legal-guide", headers=HEADERS)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == guide_document
        assert client.get("/api/v1/system", headers=HEADERS).json() == status_before
        assert client.get("/api/v1/rules", headers=HEADERS).json() == rules_before
        assert status_before["ready"] is False
        assert rules_before["enabled_count"] == 0
        openapi = client.get("/openapi.json").json()
        endpoint = openapi["paths"]["/api/v1/legal-guide"]["get"]
        assert endpoint["security"] == [{"ApiKeyAuth": []}]
        assert endpoint["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/LegalGuide")


def test_unavailable_legal_guide_is_a_structured_error_independent_of_analysis_catalog(tmp_path, monkeypatch, guide_document):
    guide_path = tmp_path / "guide.json"
    monkeypatch.setattr(legal_module, "DEFAULT_LEGAL_GUIDE", guide_path)
    settings = Settings(api_key=HEADERS["X-API-Key"], data_dir=tmp_path / "jobs", rule_catalog_path=tmp_path / "missing-rules.json")
    with TestClient(create_app(settings)) as client:
        missing = client.get("/api/v1/legal-guide", headers=HEADERS)
        assert missing.status_code == 503
        assert missing.headers["cache-control"] == "no-store"
        assert missing.json() == {"detail": {"code": "invalid_legal_guide", "message": "The local legal guide is missing or invalid"}}
        assert str(tmp_path) not in missing.text
        write_guide(guide_path, guide_document)
        assert client.get("/api/v1/legal-guide", headers=HEADERS).status_code == 200
        assert client.get("/api/v1/rules", headers=HEADERS).status_code == 503
        guide_document["procedures"][0]["source_ids"] = ["UNKNOWN"]
        write_guide(guide_path, guide_document)
        invalid = client.get("/api/v1/legal-guide", headers=HEADERS)
        assert invalid.status_code == 503
        assert invalid.json()["detail"]["code"] == "invalid_legal_guide"


@pytest.mark.parametrize("url", [
    "http://adilet.zan.kz/rus/docs/test", "https://example.org/law", "https://adilet.zan.kz.attacker.test/law",
    "https://user:password@adilet.zan.kz/law", "https://adilet.zan.kz:8443/law",
])
def test_loader_rejects_unofficial_or_unsafe_legal_source(tmp_path, guide_document, url):
    guide_document["sources"][0]["url"] = url
    path = tmp_path / "guide.json"
    write_guide(path, guide_document)
    with pytest.raises(LegalGuideUnavailableError):
        load_legal_guide(path)


@pytest.mark.parametrize("case", ["duplicate_source", "duplicate_procedure", "unknown_source", "empty_requirement", "empty_list_text", "binding"])
def test_loader_rejects_invalid_guide_and_reference_claims(tmp_path, guide_document, case):
    if case == "duplicate_source":
        guide_document["sources"].append(deepcopy(guide_document["sources"][0]))
    elif case == "duplicate_procedure":
        guide_document["procedures"].append(deepcopy(guide_document["procedures"][0]))
    elif case == "unknown_source":
        guide_document["procedures"][0]["source_ids"] = ["UNKNOWN"]
    elif case == "empty_requirement":
        guide_document["procedures"][0]["requirement"] = "   "
    elif case == "empty_list_text":
        guide_document["procedures"][0]["correct"] = ["   "]
    else:
        guide_document["result_binding"] = "applied_to_result"
    path = tmp_path / "guide.json"
    write_guide(path, guide_document)
    with pytest.raises(LegalGuideUnavailableError):
        load_legal_guide(path)
