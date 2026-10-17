from __future__ import annotations

import sys
from types import ModuleType

import pytest
from pydantic import ValidationError

from app.analyzer import (
    AnalysisContractError,
    AnalyzerConfigurationError,
    DetectedViolation,
    InsufficientEvidenceError,
    LocalAnalysisResult,
    RuleValidationError,
    UnconfiguredAnalyzer,
    load_analyzer_factory,
)
from app.evidence import materialize_result, validate_analysis_result
from app.rules import RuleCatalog, load_rule_catalog


@pytest.fixture
def verified_catalog():
    return RuleCatalog.model_validate({
        "version": "synthetic-test-only",
        "rules": [{
            "id": "TEST-RULE",
            "event_type": "Canonical event from catalog",
            "enabled": True,
            "verification": "verified",
            "verified_on": "2026-10-17",
            "reference": {
                "title": "Synthetic test reference",
                "clause": "Test clause",
                "source_url": "https://example.com/synthetic-test-only",
            },
            "observable_conditions": ["Synthetic visible condition"],
            "exclusions": ["Occluded condition"],
            "verification_note": "Synthetic fixture; not a legal or production rule",
        }],
    })


def complete_result(**overrides):
    return LocalAnalysisResult.model_validate({
        "verdict": "no_violation",
        "violations": [],
        "analysis_complete": True,
        "checked_rule_ids": ["TEST-RULE"],
        **overrides,
    })


def event(**overrides):
    return {
        "rule_id": "TEST-RULE",
        "timecode_sec": 5.0,
        "description": "Visible synthetic observation",
        "evidence_timestamps_sec": [4.0, 5.0, 6.0],
        **overrides,
    }


def test_default_rules_are_candidates_and_cannot_produce_all_clear():
    catalog = load_rule_catalog()
    assert catalog.enabled_rule_ids == ()
    with pytest.raises(InsufficientEvidenceError):
        validate_analysis_result(complete_result(), 10.0, catalog)


@pytest.mark.parametrize("overrides", [
    {"analysis_complete": False},
    {"checked_rule_ids": []},
    {"limitations": ["Client area outside frame"]},
])
def test_no_violation_requires_complete_observable_checks(verified_catalog, overrides):
    with pytest.raises(InsufficientEvidenceError):
        validate_analysis_result(complete_result(**overrides), 10.0, verified_catalog)


def test_missing_completeness_declaration_defaults_to_insufficient(verified_catalog):
    result = LocalAnalysisResult(verdict="no_violation", violations=[])
    with pytest.raises(InsufficientEvidenceError):
        validate_analysis_result(result, 10.0, verified_catalog)


def test_valid_no_violation_is_scoped_to_enabled_rules(verified_catalog):
    validate_analysis_result(complete_result(), 10.0, verified_catalog)


def test_supported_positive_survives_partial_video_coverage(tmp_path, monkeypatch, verified_catalog):
    second = verified_catalog.rules[0].model_copy(update={"id": "OTHER-RULE"})
    catalog = verified_catalog.model_copy(update={"rules": [verified_catalog.rules[0], second]})
    result = complete_result(
        verdict="violation", violations=[event()], analysis_complete=False,
        limitations=["Another procedure is not observable in part of the recording"],
    )
    monkeypatch.setattr("app.evidence.extract_jpeg", lambda *args: b"test-source-frame")
    completed = materialize_result("test", tmp_path / "source.mov", tmp_path / "frames",
                                   result, "ffmpeg", 10.0, catalog)
    assert len(completed.violations) == 1
    assert completed.analysis_scope.model_dump() == {
        "full_video_checked": False,
        "checked_rule_ids": ["TEST-RULE"],
        "enabled_rule_ids": ["TEST-RULE", "OTHER-RULE"],
        "limitations": ["Another procedure is not observable in part of the recording"],
    }


def test_positive_must_reference_a_rule_the_analyzer_checked(verified_catalog):
    result = complete_result(verdict="violation", violations=[event()], checked_rule_ids=[])
    with pytest.raises(AnalysisContractError):
        validate_analysis_result(result, 10.0, verified_catalog)


def test_model_cannot_supply_its_own_normative_reference():
    with pytest.raises(ValidationError):
        DetectedViolation.model_validate(event(rule={
            "title": "Invented law", "clause": "123", "source_url": "https://example.com/fake",
        }))


def test_unknown_rule_is_rejected_even_with_plausible_observation(verified_catalog):
    result = complete_result(verdict="violation", violations=[event(rule_id="INVENTED-RULE")])
    with pytest.raises(RuleValidationError):
        validate_analysis_result(result, 10.0, verified_catalog)


def test_disabled_rule_is_rejected(verified_catalog):
    second = verified_catalog.rules[0].model_copy(update={"id": "DISABLED-RULE", "enabled": False})
    catalog = verified_catalog.model_copy(update={"rules": [verified_catalog.rules[0], second]})
    result = complete_result(verdict="violation", violations=[event(rule_id="DISABLED-RULE")])
    with pytest.raises(RuleValidationError):
        validate_analysis_result(result, 10.0, catalog)


@pytest.mark.parametrize("overrides", [
    {"timecode_sec": 10.0},
    {"timecode_sec": 11.0},
    {"evidence_timestamps_sec": [10.0]},
    {"evidence_timestamps_sec": [5.0, 99.0]},
])
def test_out_of_source_timestamps_fail_before_any_extraction(tmp_path, monkeypatch, verified_catalog, overrides):
    def must_not_extract(*args, **kwargs):
        pytest.fail("Validation must finish before any FFmpeg call")

    monkeypatch.setattr("app.evidence.extract_jpeg", must_not_extract)
    result = complete_result(verdict="violation", violations=[event(**overrides)])
    with pytest.raises(AnalysisContractError):
        materialize_result("test", tmp_path / "source.mov", tmp_path / "frames", result,
                           "ffmpeg", 10.0, verified_catalog)


@pytest.mark.parametrize("timestamp", [-1, float("nan"), float("inf"), True])
def test_invalid_model_timestamp_is_rejected(timestamp):
    with pytest.raises(ValidationError):
        DetectedViolation.model_validate(event(timecode_sec=timestamp))


def test_enabled_candidate_rule_is_rejected(verified_catalog):
    data = verified_catalog.model_dump(mode="json")
    data["rules"][0]["verification"] = "candidate"
    with pytest.raises(ValidationError):
        RuleCatalog.model_validate(data)


def test_missing_factory_keeps_analysis_unconfigured():
    assert isinstance(load_analyzer_factory(None), UnconfiguredAnalyzer)


@pytest.mark.parametrize("spec", ["no_colon", "module:factory()", "module:factory.attr", " module:factory"])
def test_factory_spec_is_not_executable_text(spec):
    with pytest.raises(AnalyzerConfigurationError):
        load_analyzer_factory(spec)


def test_factory_checks_callable_analyze(monkeypatch):
    module = ModuleType("synthetic_test_adapter")
    module.build = lambda: object()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(AnalyzerConfigurationError):
        load_analyzer_factory("synthetic_test_adapter:build")


def test_factory_returns_local_adapter(monkeypatch):
    class Adapter:
        def analyze(self, context):
            return complete_result()

    module = ModuleType("synthetic_test_adapter")
    adapter = Adapter()
    module.build = lambda: adapter
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert load_analyzer_factory("synthetic_test_adapter:build") is adapter
