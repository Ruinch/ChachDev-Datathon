from ml_tools.quality import score_anchors, by_view, negative_diagnostics
import pytest


def test_wrong_event_type_does_not_count_as_a_human_note_match():
    rows = [dict(id='synthetic', sha256='hash', status='done')]
    targets = {'synthetic': dict(sha256='hash', source_role='human', interpretation_reviewed=False,
                                point_sec=10, candidate_rule_ids=['R20-C'])}
    predictions = {'synthetic': {'violations': [dict(rule_id='R22', timecode_sec=10)]}}
    result = score_anchors(rows, predictions, targets)
    assert result['note_anchors'] == 1 and result['compatible_type_and_point'] == 0
    predictions['synthetic']['violations'] = [dict(rule_id='R20-C', timecode_sec=20)]
    result = score_anchors(rows, predictions, targets)
    assert result['compatible_type'] == 1 and result['compatible_type_and_point'] == 0
    predictions['synthetic']['violations'][0]['timecode_sec'] = 12
    assert score_anchors(rows, predictions, targets)['compatible_type_and_point'] == 1


def test_changed_video_cannot_reuse_a_note_anchor():
    with pytest.raises(ValueError, match='hash'):
        score_anchors([dict(id='a', sha256='new', status='failed')], {},
                      {'a': dict(sha256='old')})


def test_view_diagnostics_preserve_unverified_status_and_count_failures():
    rows = [dict(sha256='a', status='failed', label='no_violation', elapsed_sec=2)]
    result = by_view(rows, {'human_verified':False, 'records':[dict(sha256='a',view_group='proposal-1')]})
    assert result['human_verified'] is False
    assert result['groups']['proposal-1']['failed'] == 1
    assert result['groups']['proposal-1']['tn'] == 0


def test_negative_checks_count_extra_events_and_keep_failures_separate():
    rows = [dict(id='a', label='no_violation', status='done'),
            dict(id='b', label='no_violation', status='failed'),
            dict(id='c', label='violation', status='done')]
    predictions = {'a': {'violations': [dict(rule_id='R22'), dict(rule_id='R20-C')]},
                   'c': {'violations': [dict(rule_id='R20-A')]}}
    summary = negative_diagnostics(rows, predictions)
    assert summary['negative_videos'] == 2 and summary['completed'] == 1 and summary['failed'] == 1
    assert summary['videos_with_alarms'] == 1
    assert summary['false_events_by_rule'] == {'R22': 1, 'R20-C': 1}
