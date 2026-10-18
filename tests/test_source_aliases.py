from __future__ import annotations

import hashlib
import itertools
import json
import sys

import pytest

from app.analyzer import DetectedViolation, LocalAnalysisResult
from ml_tools import evaluate
from ml_tools.annotations import validate_annotations
from ml_tools.source_aliases import resolve_annotations_by_sha, select_unique_sources


def record(identifier, **updates):
    return dict(id=identifier, sha256='synthetic-source', split='dev', duration_sec=8,
                label='violation', point_sec=2, **updates)


def reviewed(*events, verdict='violation'):
    return dict(sha256='synthetic-source', reviewed=True, source_group='synthetic-session',
                verdict=verdict, events=list(events))


EVENT = dict(rule_id='R20-A', start_sec=1, end_sec=3)


@pytest.mark.parametrize('conflict', [reviewed(verdict='no_violation'),
                                     reviewed(dict(EVENT, rule_id='R22')),
                                     reviewed(dict(EVENT, end_sec=4)),
                                     reviewed(EVENT, verdict='uncertain')])
def test_reviewed_alias_conflicts_fail_regardless_of_manifest_or_annotation_order(conflict):
    records = [record('a'), record('z')]
    annotations = {'a': reviewed(EVENT), 'z': conflict}
    for order in itertools.permutations(records):
        for entries in itertools.permutations(annotations.items()):
            with pytest.raises(ValueError, match='conflicting reviewed'):
                validate_annotations({'records': list(order)}, dict(entries))


def test_reviewed_alias_survives_missing_and_unreviewed_aliases_without_changing_truth():
    records = [record('a-missing'), record('b-draft'), record('z-reviewed')]
    truth = reviewed(EVENT)
    annotations = {'b-draft': dict(reviewed(verdict='no_violation'), reviewed=False), 'z-reviewed': truth}
    for order in itertools.permutations(records):
        assert resolve_annotations_by_sha(list(order), annotations)['synthetic-source'] is truth


def test_event_order_and_free_text_do_not_create_conflicting_scoring_truth():
    other_event = dict(rule_id='R22', start_sec=4, end_sec=6)
    annotations = {'a': dict(reviewed(EVENT, other_event), notes='synthetic first note'),
                   'z': dict(reviewed(other_event, dict(EVENT, description='synthetic description')), notes='another note')}
    assert validate_annotations({'records': [record('a'), record('z')]}, annotations)['reviewed'] == 2
    assert resolve_annotations_by_sha([record('z'), record('a')], annotations)['synthetic-source'] is annotations['a']


@pytest.mark.parametrize('field,value', [('label', 'no_violation'), ('duration_sec', 9), ('point_sec', 3)])
def test_conflicting_source_metadata_requires_reconciliation_or_quarantine(field, value):
    records = [record('a'), dict(record('z'), **{field: value})]
    for order in itertools.permutations(records):
        with pytest.raises(ValueError, match='conflicting labels, durations or point hints'):
            select_unique_sources(list(order), {'dev'}, ['z'])
    records.append(dict(record('quarantined'), split='quarantine'))
    assert select_unique_sources(records, {'dev'}, ['a']) == []


def test_unique_hash_records_keep_frozen_order_metadata_and_identity():
    records = [dict(record('z'), sha256='hash-2'), dict(record('a'), sha256='hash-1')]
    selected = select_unique_sources(records, {'dev'})
    assert selected == records
    assert all(left is right for left, right in zip(selected, records))


def test_id_of_an_alias_outside_selected_split_still_selects_eligible_source_once():
    eligible = record('a')
    alias = dict(record('z'), split='holdout')
    assert select_unique_sources([alias, eligible], {'dev'}, ['z']) == [eligible]


def test_absent_point_hint_equals_none_but_never_a_known_hint():
    absent = record('a')
    del absent['point_sec']
    unknown = dict(record('z'), point_sec=None)
    assert select_unique_sources([unknown, absent], {'dev'}) == [absent]
    with pytest.raises(ValueError, match='point hints'):
        select_unique_sources([absent, record('z')], {'dev'})


def test_unknown_requested_id_does_not_fall_back_to_whole_dataset():
    assert select_unique_sources([record('a')], {'dev'}, ['unknown']) == []


def test_reviewed_uncertain_truth_stays_uncertain_and_is_not_scored():
    truth = reviewed(EVENT, verdict='uncertain')
    annotations = {'a': dict(reviewed(EVENT), reviewed=False), 'z': truth}
    resolved = resolve_annotations_by_sha([record('a'), record('z')], annotations)['synthetic-source']
    assert resolved is truth
    assert evaluate.score_supported_events(resolved, [], ('R20-A',)) is None


def test_quarantine_excludes_source_but_does_not_certify_conflicting_reviewed_truth():
    records = [record('a'), dict(record('z'), split='quarantine')]
    assert select_unique_sources(records, {'dev'}) == []
    with pytest.raises(ValueError, match='conflicting reviewed'):
        validate_annotations({'records': records}, {'a': reviewed(EVENT), 'z': reviewed(verdict='no_violation')})


@pytest.mark.parametrize('ids', [['a'], ['z'], ['a', 'z']])
@pytest.mark.parametrize('reverse', [False, True])
def test_cli_alias_ids_select_one_canonical_source_and_inherit_reviewed_truth(
        tmp_path, monkeypatch, isolated_evaluation_lock, ids, reverse):
    source = tmp_path / 'synthetic.mov'
    source.write_bytes(b'synthetic duplicate source; not a video')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    records = [dict(record(key), path=str(source), sha256=digest) for key in ('a', 'z')]
    if reverse:
        records.reverse()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'records': records}))
    references = tmp_path / 'annotations.json'
    references.write_text(json.dumps({'annotations': {
        'a': dict(reviewed(verdict='no_violation'), sha256=digest, reviewed=False),
        'z': dict(reviewed(EVENT), sha256=digest),
    }}))
    output = tmp_path / 'evaluation'
    calls = []

    class Analyzer:
        def __init__(self, config):
            pass

        def analyze_with_metrics(self, context):
            calls.append(context.job_id)
            return LocalAnalysisResult(verdict='violation', violations=[DetectedViolation(
                rule_id='R20-A', timecode_sec=2, description='Synthetic source observation',
                evidence_timestamps_sec=[2],
            )]), {'elapsed_sec': 0.1}

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(evaluate.MemorySampler, 'run', lambda self: self.stop.wait(2))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--manifest', str(manifest), '--annotations', str(references),
                                    '--output', str(output), '--mode', 'dev', '--ids', *ids])
    evaluate.main()
    report = json.loads((output / 'metrics.json').read_text())
    assert calls == ['a']
    assert report['summary']['videos'] == report['summary']['tp'] == 1
    assert report['event_metrics_completed_only'] == dict(tp=1, fp=0, fn=0, reviewed_videos=1)
    assert report['reference_scope']['reviewed_scorable_videos'] == 1
    assert report['reference_scope']['draft_videos'] == 0
