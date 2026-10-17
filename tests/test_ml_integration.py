"""ML contract regressions using synthetic frames; no dataset or weights in tests."""
import time
from pathlib import Path

import pytest

from app.analyzer import AnalysisContext, InsufficientEvidenceError, load_analyzer_factory
from app.evidence import validate_analysis_result
from app.local_analyzer import AnalyzerConfig, QwenLocalAnalyzer
from app.ml.model import Event, WindowResult
from app.ml.rules import RULES
from app.ml.video import Frame, Video, VideoAnalysisError
from app.rules import load_rule_catalog
from ml_tools.reconcile_labels import review_seed


@pytest.fixture
def decoded(monkeypatch):
    video = Video(36, tuple(Frame(float(i), b'jpeg') for i in range(36)), 1080)
    monkeypatch.setattr('app.local_analyzer.decode_video', lambda *a, **k: video)


def detection(frames):
    return Event(rule_id='R22', start_frame=0, peak_frame=1, end_frame=2,
                 conditions=list(RULES['R22'].criteria), observation='Synthetic visible event for contract validation.')


def test_confirmed_event_survives_other_unassessable_window(decoded, synthetic_ml_catalog):
    class PartialModel:
        def infer(self, frames, timeout, *, refine=False, **kw):
            if refine or frames[0].timestamp == 0:
                return WindowResult(assessable=True, events=[detection(frames)])
            return WindowResult(assessable=False, events=[])
    result = QwenLocalAnalyzer(model=PartialModel()).analyze(AnalysisContext('test', Path('test.mov'), 36, ('R22',)))
    assert result.verdict == 'violation' and result.violations
    assert result.analysis_complete is False and result.limitations
    validate_analysis_result(result, 36, synthetic_ml_catalog)


def test_complete_negative_requires_all_configured_windows(decoded):
    calls = []
    class Visible:
        def infer(self, frames, timeout, **kwargs):
            calls.append((frames[0].timestamp, frames[-1].timestamp, kwargs['rule_ids']))
            return WindowResult(assessable=True, events=[])
    result = QwenLocalAnalyzer(model=Visible()).analyze(AnalysisContext('test', Path('test.mov'), 36, ('R20-A',)))
    assert result.verdict == 'no_violation' and result.analysis_complete
    assert result.checked_rule_ids == ['R20-A'] and not result.limitations
    assert len(calls) == 3  # global plus two overlapping windows including the tail
    assert calls[-1][1] == 35


def test_unknown_enabled_procedure_fails_before_model(decoded):
    with pytest.raises(VideoAnalysisError, match='support'):
        QwenLocalAnalyzer().analyze(AnalysisContext('test', Path('test.mov'), 36, ('UNKNOWN',)))


def test_empty_scope_does_not_produce_all_clear(decoded):
    with pytest.raises(InsufficientEvidenceError):
        QwenLocalAnalyzer().analyze(AnalysisContext('test', Path('test.mov'), 36))


def test_service_factory_is_loadable_and_never_downloads(monkeypatch):
    monkeypatch.setenv('APP_LOCAL_MODEL_URL', 'http://127.0.0.1:8091')
    assert isinstance(load_analyzer_factory('app.local_analyzer:build_analyzer'), QwenLocalAnalyzer)
    monkeypatch.setenv('APP_LOCAL_MODEL_URL', 'https://example.com')
    from app.analyzer import AnalyzerConfigurationError
    with pytest.raises(AnalyzerConfigurationError):
        load_analyzer_factory('app.local_analyzer:build_analyzer')


def test_ai_intervals_are_never_promoted_to_reviewed_truth():
    data = {'records': [{'id': 'synthetic', 'sha256': 'synthetic-hash',
        'human': {'note': 'Manual observation', 'label': 'violation', 'point_sec': 7},
        'ai': {'summary': 'Disagreeing AI suggestion', 'proposed_events': [dict(
            rule_id='R22', start_sec=1, end_sec=2, observation='AI text')]}}]}
    record = review_seed(data)['annotations']['synthetic']
    assert record['reviewed'] is False and record['events'] == []
    assert record['verdict'] == 'uncertain'
    assert record['notes'].index('Manual observation') < record['notes'].index('Disagreeing AI suggestion')


def test_catalog_ids_and_model_procedures_agree():
    catalog = load_rule_catalog(Path('app/data/ml-rules.json'))
    assert {rule.id for rule in catalog.rules} == set(RULES)
    assert catalog.enabled_rule_ids == ()  # Failed quality evaluation must not auto-enable deployment.
    assert load_rule_catalog().enabled_rule_ids == ()


def test_human_workbook_overrides_conflicting_ai_without_changing_inputs(tmp_path):
    Workbook = pytest.importorskip('openpyxl', reason='Install .[ml,test] for workbook import checks').Workbook
    from ml_tools.reconcile_labels import digest, reconcile
    human_path, ai_path = tmp_path / 'human.xlsx', tmp_path / 'ai.xlsx'
    human = Workbook()
    sheet = human.active
    sheet.title = 'labels'
    sheet.append(['file', 'point', 'label', 'note'])
    sheet.append(['X-1.mov', 7, 'нарушение', 'Primary handwritten note'])
    human.save(human_path)
    ai = Workbook()
    sheet = ai.active
    sheet.title = 'Реестр видео'
    for _ in range(5):
        sheet.append(['header'])
    sheet.append([1, 'X-1.mov', 'без нарушения', 2, 10, 'category', 'R22', 'Conflicting AI description'])
    sheet = ai.create_sheet('События и гипотезы')
    for _ in range(5):
        sheet.append(['header'])
    sheet.append([1, 'X-1.mov', '', 'category', 'R22', '', 'AI observation', 'Unknown context', '', 0, 10, 1, 2])
    ai.save(ai_path)
    hashes = (digest(human_path), digest(ai_path))
    manifest = {'records': [{'id': 'X-1', 'sha256': 'synthetic', 'duration_sec': 10, 'split': 'dev'}]}
    result = reconcile(manifest, human_path, ai_path)
    row = result['records'][0]
    assert row['human']['label'] == 'violation' and row['human']['point_sec'] == 7
    assert row['preferred_observation'] == 'Primary handwritten note'
    assert row['preferred_observation_source'] == 'human'
    assert row['ai']['proposed_events'][0]['human_verified'] is False
    assert result['summary']['human_verified_intervals'] == 0
    assert hashes == (digest(human_path), digest(ai_path))
