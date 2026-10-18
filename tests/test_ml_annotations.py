from __future__ import annotations

import pytest

from ml_tools.annotations import normalize_source_group, validate_annotations
from ml_tools.reconcile_labels import reconcile, review_seed


def manifest():
    return {'records': [dict(id='X-1', sha256='synthetic-a', split='dev', duration_sec=10)]}


def test_import_preserves_human_priority_without_inventing_camera_groups(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    human = openpyxl.Workbook()
    human.active.title = 'labels'
    human.active.append(['file', 'point', 'label', 'notes'])
    human.active.append(['X-1.mov', '00:04', 'нарушение', 'Synthetic human observation'])
    human_path = tmp_path / 'human.xlsx'
    human.save(human_path)
    human.close()
    ai = openpyxl.Workbook()
    ai.active.title = 'Реестр видео'
    ai.active.cell(6, 2, 'X-1.mov')
    ai.active.cell(6, 8, 'Conflicting synthetic AI observation')
    events = ai.create_sheet('События и гипотезы')
    for column, value in {2: 'X-1.mov', 5: 'R22', 7: 'AI proposal', 12: 2, 13: 8}.items():
        events.cell(6, column, value)
    ai_path = tmp_path / 'ai.xlsx'
    ai.save(ai_path)
    ai.close()
    before = human_path.read_bytes(), ai_path.read_bytes()

    result = reconcile(manifest(), human_path, ai_path)
    row = result['records'][0]
    assert row['preferred_observation'] == 'Synthetic human observation'
    assert row['preferred_observation_source'] == 'human'
    assert row['human']['point_sec'] == 4
    assert row['ai']['proposed_events'][0]['human_verified'] is False
    seed = review_seed(result)['annotations']['X-1']
    assert seed['source_group'] == seed['viewpoint'] == ''
    assert seed['reviewed'] is False and seed['events'] == []
    assert seed['verdict'] == 'uncertain'
    assert seed['notes'].index('Synthetic human observation') < seed['notes'].index('Conflicting synthetic AI observation')
    assert (human_path.read_bytes(), ai_path.read_bytes()) == before


@pytest.mark.parametrize('group', ['', '  ', 'organizer-single-camera', 'unknown-session', 'unknown', 'unassigned'])
def test_unverified_camera_group_cannot_be_finalized(group):
    annotation = dict(sha256='synthetic-a', reviewed=True, source_group=group,
                      verdict='no_violation', events=[])
    with pytest.raises(ValueError, match='group|placeholder'):
        validate_annotations(manifest(), {'X-1': annotation}, require_all=True)


def test_legacy_import_remains_readable_as_an_unreviewed_draft():
    annotation = dict(sha256='synthetic-a', reviewed=False,
                      source_group='organizer-single-camera', verdict='uncertain', events=[])
    assert validate_annotations(manifest(), {'X-1': annotation})['reviewed'] == 0


def test_distinct_reviewed_source_groups_can_be_finalized():
    dataset = manifest()
    dataset['records'].append(dict(id='X-2', sha256='synthetic-b', split='holdout', duration_sec=12))
    annotations = {r['id']: dict(sha256=r['sha256'], reviewed=True, source_group=f'session-{i}',
                                 viewpoint='Synthetic view', verdict='no_violation', events=[])
                   for i, r in enumerate(dataset['records'])}
    assert validate_annotations(dataset, annotations, require_all=True) == dict(
        reviewed=2, source_groups=2, leaking_groups=0)


@pytest.mark.parametrize('groups', [(' Session-A ', 'session-a'), ('STRA\u00dfE', 'strasse')])
def test_group_case_and_whitespace_cannot_hide_split_leakage(groups):
    dataset = manifest()
    dataset['records'].append(dict(id='X-2', sha256='synthetic-b', split='holdout', duration_sec=12))
    annotations = {r['id']: dict(sha256=r['sha256'], reviewed=True, source_group=group,
                                 verdict='no_violation', events=[])
                   for r, group in zip(dataset['records'], groups)}
    assert validate_annotations(dataset, annotations)['leaking_groups'] == 1
    with pytest.raises(ValueError, match='Source groups overlap'):
        validate_annotations(dataset, annotations, require_all=True)
    assert tuple(a['source_group'] for a in annotations.values()) == groups


def test_case_variants_of_duplicate_source_group_are_not_false_conflicts():
    dataset = manifest()
    dataset['records'].append(dict(id='X-2', sha256='synthetic-a', split='dev', duration_sec=10))
    annotations = {r['id']: dict(sha256=r['sha256'], reviewed=True, source_group=group,
                                 verdict='no_violation', events=[])
                   for r, group in zip(dataset['records'], (' Session-A ', 'SESSION-a'))}
    assert validate_annotations(dataset, annotations, require_all=True) == dict(
        reviewed=2, source_groups=1, leaking_groups=0)


@pytest.mark.parametrize('value', [None, 1, True, [], {}])
def test_source_group_requires_a_string(value):
    with pytest.raises(ValueError, match='source group must be a string'):
        normalize_source_group(value)


def test_placeholder_normalization_does_not_make_unknown_group_reviewed():
    annotation = dict(sha256='synthetic-a', reviewed=True, source_group=' UnKnOwN ',
                      verdict='no_violation', events=[])
    with pytest.raises(ValueError, match='placeholder'):
        validate_annotations(manifest(), {'X-1': annotation})
