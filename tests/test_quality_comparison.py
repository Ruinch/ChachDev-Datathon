import pytest
from ml_tools.compare_quality import compare_reports, wilson_interval, repeatability

def row(key,label,verdict=None):
    return dict(id=key,sha256=key,label=label,status='done' if verdict else 'failed',verdict=verdict,
                error_code=None if verdict else 'insufficient_evidence',elapsed_sec=2,event_times=[])

def test_abstention_is_not_a_correct_negative_or_a_reported_improvement():
    old={'rows':[row('a','no_violation','violation'),row('b','violation','violation')]}
    new={'rows':[row('a','no_violation'),row('b','violation','no_violation')]}
    result=compare_reports(old,new)
    assert result['paired']['improved']==0
    assert result['paired']['regressed']==1
    assert result['candidate']['failed']==1 and result['candidate']['tn']==0

def test_even_perfect_tiny_sample_has_wide_confidence_interval():
    assert wilson_interval(0,0) is None
    low,high=wilson_interval(4,4)
    assert low==pytest.approx(0.5101,abs=0.0001)
    assert high==pytest.approx(1.0)

def test_comparison_requires_identical_sources_and_labels():
    with pytest.raises(ValueError):
        compare_reports({'rows':[row('a','violation')]},{'rows':[row('b','violation')]})
    with pytest.raises(ValueError):
        compare_reports({'rows':[row('a','violation')]},{'rows':[row('a','no_violation')]})

def test_duplicate_sources_cannot_inflate_confidence():
    with pytest.raises(ValueError):
        compare_reports({'rows':[row('a','violation'),row('a','violation')]},{'rows':[row('a','violation')]})


@pytest.mark.parametrize('before,after', [(3, 7), (None, 3), (3, None)])
def test_paired_comparison_rejects_changed_reference_time(before, after):
    old = {'rows': [dict(row('a', 'violation', 'violation'), point_sec=before, event_times=[3])]}
    new = {'rows': [dict(row('a', 'violation', 'violation'), point_sec=after, event_times=[3])]}
    with pytest.raises(ValueError, match='Reference time points changed'):
        compare_reports(old, new)
    with pytest.raises(ValueError, match='Reference time points changed'):
        repeatability(old, new, {}, {})


def test_equivalent_reference_time_and_legacy_missing_point_remain_comparable():
    old = {'rows': [dict(row('a', 'violation', 'violation'), point_sec=3, event_times=[3]),
                    row('b', 'no_violation', 'no_violation')]}
    new = {'rows': [dict(row('a', 'violation', 'violation'), point_sec=3.0, event_times=[3]),
                    dict(row('b', 'no_violation', 'no_violation'), point_sec=None)]}
    assert compare_reports(old, new)['paired']['unchanged_correctness'] == 2


def test_repeatable_wrong_answer_is_still_wrong_not_high_confidence():
    rows=[row('a','no_violation','violation')]
    predictions={'a':{'violations':[{'rule_id':'R22','timecode_sec':3}]}}
    r=repeatability({'rows':rows},{'rows':rows},predictions,predictions)
    assert r['stable_completed_predictions']==1
    assert r['stable_but_binary_wrong']==1
    assert r['stable_and_binary_correct']==0


def test_changed_class_is_unstable_even_when_binary_verdict_agrees():
    rows=[row('a','violation','violation')]
    a={'a':{'violations':[{'rule_id':'R22','timecode_sec':3}]}}
    b={'a':{'violations':[{'rule_id':'R20-A','timecode_sec':3}]}}
    assert repeatability({'rows':rows},{'rows':rows},a,b)['stable_completed_predictions']==0
