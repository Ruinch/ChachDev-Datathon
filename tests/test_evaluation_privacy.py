import hashlib
import json
import sys
from pathlib import Path

import pytest

from app.analyzer import LocalAnalysisResult
from ml_tools import evaluate


@pytest.mark.parametrize('reclassify', [False, True])
def test_evaluation_stdout_is_aggregate_and_research_config_is_recorded(tmp_path, monkeypatch, capsys, reclassify,
                                                                     isolated_evaluation_lock):
    source = tmp_path / 'fixture.mov'
    source.write_bytes(b'synthetic source; decoding is a separate test boundary')
    private_id = 'private-record-must-not-appear-in-stdout'
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'records': [dict(
        id=private_id, path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        label='no_violation', point_sec=None, duration_sec=3, split='dev')]}))
    output = tmp_path / 'results'
    seen = []
    class Analyzer:
        def __init__(self, config):
            seen.append(config)
        def analyze_with_metrics(self, context):
            return LocalAnalysisResult(verdict='no_violation', violations=[], checked_rule_ids=list(context.enabled_rule_ids)), {'elapsed_sec': 0.1}
    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(evaluate.MemorySampler, 'run', lambda self: self.stop.wait(2))
    args = ['evaluate', '--manifest', str(manifest), '--output', str(output), '--mode', 'dev']
    if reclassify:
        args.append('--allow-reclassification')
    monkeypatch.setattr(sys, 'argv', args)
    evaluate.main()
    stdout = capsys.readouterr().out
    assert private_id not in stdout
    assert str(source) not in stdout
    assert seen[0].allow_reclassification is reclassify
    report = json.loads((output / 'metrics.json').read_text())
    assert report['rows'][0]['id'] == private_id  # Full detail remains local.
    assert report['configuration']['allow_reclassification'] is reclassify


def test_all_video_diagnostic_excludes_quarantine_and_duplicates_and_records_order(tmp_path, monkeypatch,
                                                                                isolated_evaluation_lock):
    records = []
    for name, split in [('one', 'dev'), ('two', 'holdout'), ('conflict', 'quarantine')]:
        source = tmp_path / (name + '.mov')
        source.write_bytes(name.encode())
        records.append(dict(id=name, path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                            label='no_violation', point_sec=None, duration_sec=3, split=split))
    records.append(dict(records[0], id='duplicate'))
    records.append(dict(records[2], id='conflicting-duplicate', split='dev'))
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'records': records}))
    output = tmp_path / 'results'
    class Analyzer:
        def __init__(self, config):
            pass
        def analyze_with_metrics(self, context):
            return LocalAnalysisResult(verdict='no_violation', violations=[], checked_rule_ids=list(context.enabled_rule_ids)), {'elapsed_sec': 0.1}
    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(evaluate.MemorySampler, 'run', lambda self: self.stop.wait(2))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--manifest', str(manifest), '--output', str(output),
                                    '--mode', 'diagnostic-all', '--order', 'sha256'])
    evaluate.main()
    report = json.loads((output/'metrics.json').read_text())
    hashes = [r['sha256'] for r in report['rows']]
    assert hashes == sorted({records[0]['sha256'], records[1]['sha256']})
    assert report['order'] == 'sha256'
    assert 'not a held-out test' in report['evaluation_scope']


@pytest.mark.parametrize('failure', ['missing', 'unreadable', 'read_error', 'hash_mismatch'])
def test_source_integrity_failure_is_sanitized_skips_inference_and_continues(
        tmp_path, monkeypatch, capsys, isolated_evaluation_lock, failure):
    private_id = 'private-unavailable-record'
    private_note = 'Synthetic private annotation must remain local'
    source = tmp_path / 'private-unavailable-source.mov'
    source.write_bytes(b'synthetic unavailable source')
    good = tmp_path / 'readable.mov'
    good.write_bytes(b'synthetic readable source')
    records = [dict(id=identifier, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    label=label, duration_sec=duration, split='dev')
               for identifier, path, label, duration in [(private_id, source, 'violation', 3),
                                                         ('readable', good, 'no_violation', 4)]]
    references = tmp_path / 'annotations.json'
    references.write_text(json.dumps({'annotations': {private_id: dict(
        sha256=records[0]['sha256'], reviewed=True, source_group='synthetic-source-group',
        verdict='violation', notes=private_note, events=[dict(rule_id='R20-A', start_sec=1, end_sec=2)],
    )}}))
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'records': records}))
    output = tmp_path / 'results'
    if failure == 'missing':
        source.unlink()
    elif failure == 'unreadable':
        original_open = Path.open

        def restricted_open(path, *args, **kwargs):
            if path == source:
                raise PermissionError(f'Private OS error: {source}; {private_note}')
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'open', restricted_open)
    elif failure == 'read_error':
        original_digest = hashlib.file_digest

        def interrupted_digest(file, *args, **kwargs):
            if Path(file.name) == source:
                raise OSError(f'Private storage read error: {source}; {private_note}')
            return original_digest(file, *args, **kwargs)

        monkeypatch.setattr(hashlib, 'file_digest', interrupted_digest)
    else:
        source.write_bytes(b'changed after synthetic manifest audit')
    calls = []

    class Analyzer:
        def __init__(self, config):
            pass

        def analyze_with_metrics(self, context):
            calls.append(context.job_id)
            return LocalAnalysisResult(verdict='no_violation', violations=[]), {'elapsed_sec': 0.1}

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(evaluate.MemorySampler, 'run', lambda self: self.stop.wait(2))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--manifest', str(manifest), '--annotations', str(references),
                                    '--output', str(output), '--mode', 'dev'])
    evaluate.main()
    report = json.loads((output / 'metrics.json').read_text())
    assert calls == ['readable']
    failed, completed = report['rows']
    assert failed['status'] == 'failed' and failed['error_code'] == 'local_analysis_failed'
    assert 'verdict' not in failed and failed['point_sec'] is None
    assert not (output / f'{private_id}.result.json').exists()
    assert completed['status'] == 'done' and completed['point_sec'] is None
    assert report['summary']['failed'] == report['summary']['technical_failed'] == 1
    assert report['summary']['tn'] == 1 and report['summary']['recall_over_all_positives'] == 0
    assert report['reference_scope']['supported_reference_events_on_failed_videos'] == 1
    assert report['reference_scope']['supported_event_recall_over_processed_scorable_references'] == 0
    captured = capsys.readouterr()
    for private in (private_id, str(source), private_note):
        assert private not in captured.out + captured.err
    assert str(source) not in failed['error'] and private_note not in failed['error']
