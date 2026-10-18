import hashlib
import json
import sys

from app.analyzer import DetectedViolation, InsufficientEvidenceError, LocalAnalysisResult
from ml_tools import evaluate


def event(rule_id):
    return {'rule_id': rule_id, 'start_sec': 1.0, 'end_sec': 2.0}


def annotation(*rules, reviewed=True, verdict='violation'):
    return {'reviewed': reviewed, 'verdict': verdict, 'events': [event(rule) for rule in rules]}


def test_supported_matching_excludes_other_but_keeps_wrong_procedure_false_alarm():
    reference = annotation('R20-A', 'OTHER')
    predictions = [{'rule_id': 'R20-A', 'timecode_sec': 1.5},
                   {'rule_id': 'R22', 'timecode_sec': 1.5}]
    assert evaluate.score_supported_events(reference, predictions, ('R20-A', 'R22')) == {
        'tp': 1, 'fp': 1, 'fn': 0}
    assert evaluate.score_supported_events(annotation('OTHER'), [], ('R20-A',)) == {
        'tp': 0, 'fp': 0, 'fn': 0}
    assert evaluate.score_supported_events(annotation('R20-A', reviewed=False), [], ('R20-A',)) is None
    assert evaluate.score_supported_events(annotation('R20-A', verdict='uncertain'), [], ('R20-A',)) is None


def test_scope_counts_only_selected_sources_and_keeps_failed_references_visible():
    annotations = {
        'complete': annotation('R20-A', 'OTHER', 'R22'),
        'failed': annotation('R20-A', 'OTHER'),
        'draft': annotation('R20-A', reviewed=False),
        'uncertain': annotation('R20-A', verdict='uncertain'),
        'pending': annotation('R20-A'),
        'not-selected': annotation('R20-A'),
    }
    selected = [{'id': key} for key in ('complete', 'failed', 'draft', 'uncertain', 'pending', 'missing')]
    rows = [{'id': 'complete', 'status': 'done'}, {'id': 'failed', 'status': 'failed'}]
    audit = evaluate.audit_reference_scope(selected, annotations, ('R20-A',), rows)
    assert audit['selected_videos'] == 6
    assert audit['missing_annotation_videos'] == 1
    assert audit['draft_videos'] == audit['draft_events'] == 1
    assert audit['reviewed_uncertain_videos'] == audit['reviewed_uncertain_events'] == 1
    assert audit['reviewed_scorable_videos'] == 3
    assert audit['supported_reference_events'] == 3
    assert audit['outside_catalog_reference_events'] == 3  # R22 is outside this selected catalog too.
    assert audit['other_reference_events'] == 2
    assert audit['completed_reviewed_scorable_videos'] == 1
    assert audit['failed_reviewed_scorable_videos'] == 1
    assert audit['pending_reviewed_scorable_videos'] == 1
    assert audit['supported_reference_events_on_failed_videos'] == 1
    assert audit['outside_catalog_reference_events_on_failed_videos'] == 1


def test_cli_report_keeps_broad_task_misses_and_failures_when_other_is_not_class_fn(tmp_path, monkeypatch, capsys,
                                                                                isolated_evaluation_lock):
    records = []
    annotations = {}
    cases = {
        'matched': annotation('R20-A', 'OTHER'),
        'outside': annotation('OTHER'),
        'draft': annotation('R20-A', reviewed=False),
        'uncertain': annotation('R20-A', verdict='uncertain'),
        'failed': annotation('R20-A'),
    }
    for name, value in cases.items():
        source = tmp_path / f'{name}.mov'
        source.write_bytes(f'synthetic {name}'.encode())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        records.append(dict(id=name, path=str(source), sha256=digest, label='violation',
                            point_sec=None, duration_sec=4, split='dev'))
        annotations[name] = dict(value, sha256=digest, source_group=f'fixture-group-{name}')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'records': records}))
    references = tmp_path / 'annotations.json'
    references.write_text(json.dumps({'annotations': annotations}))
    output = tmp_path / 'results'

    class Analyzer:
        def __init__(self, config):
            pass

        def analyze_with_metrics(self, context):
            if context.job_id == 'failed':
                raise InsufficientEvidenceError('synthetic lack of context')
            events = [DetectedViolation(rule_id='R20-A', timecode_sec=1.5,
                      description='Synthetic fixture observation', evidence_timestamps_sec=[1.5])] if context.job_id == 'matched' else []
            return LocalAnalysisResult(verdict='violation' if events else 'no_violation', violations=events), {'elapsed_sec': 0.1}

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(evaluate.MemorySampler, 'run', lambda self: self.stop.wait(2))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--manifest', str(manifest), '--annotations', str(references),
                                    '--output', str(output), '--mode', 'dev'])
    evaluate.main()
    report = json.loads((output / 'metrics.json').read_text())
    assert report['event_metrics_completed_only'] == {'tp': 1, 'fp': 0, 'fn': 0, 'reviewed_videos': 2}
    assert {key: report['summary'][key] for key in ('tp', 'fp', 'tn', 'fn', 'failed')} == {
        'tp': 1, 'fp': 0, 'tn': 0, 'fn': 3, 'failed': 1}
    assert report['summary']['recall_over_all_positives'] == 0.2
    scope = report['reference_scope']
    assert scope['outside_catalog_reference_events'] == scope['other_reference_events'] == 2
    assert scope['supported_reference_events_on_failed_videos'] == 1
    assert scope['supported_event_recall_over_processed_scorable_references'] == 0.5
    assert 'OTHER' in scope['notice'] and 'unchanged' in scope['notice']
    stdout = capsys.readouterr().out
    assert str(source) not in stdout
    assert 'Synthetic fixture observation' not in stdout
