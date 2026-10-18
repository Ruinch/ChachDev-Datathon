from dataclasses import replace

import pytest

from app.local_analyzer import Candidate, same_candidate
from app.ml.video import Frame
from ml_tools.episode_matching import admissible_trigger_ranges, same_episode


def candidate(start=5, end=None, peak=None, *, pts=(0, 4, 5, 6, 23), rule_id='R20-A'):
    end = start if end is None else end
    peak = start if peak is None else peak
    frames = tuple(Frame(float(t), b'synthetic-not-decoded') for t in pts)
    return Candidate(rule_id, start, end, peak, 'Synthetic event.',
                     frames[0].timestamp if frames else 0,
                     frames[-1].timestamp if frames else 0, frames, sample_tolerance=9)


def test_broad_refined_interval_cannot_confirm_a_remote_trigger():
    original = candidate()
    refined = candidate(0, 23, 23)
    assert same_candidate(original, refined)  # Reproduces the existing structural bypass.
    assert not same_episode(original, refined)


def test_remote_sampling_gap_cannot_widen_a_dense_trigger_neighborhood():
    original = candidate(.5, pts=(0, .5, 1, 19))
    assert admissible_trigger_ranges(original) == ((.25, .75),)
    assert not same_episode(original, candidate(0, 19, 5.5))
    assert same_episode(original, candidate(.75))


def test_refined_trigger_can_move_within_original_event_interval():
    original = candidate(2, 6, 2, pts=(0, 2, 4, 6, 8))
    assert admissible_trigger_ranges(original) == ((1, 7),)
    assert same_episode(original, candidate(0, 23, 5))


def test_window_edge_uses_nearest_inner_spacing_not_a_distant_gap():
    original = candidate(18, pts=(18, 20, 22, 40))
    assert admissible_trigger_ranges(original) == ((17, 19),)
    assert same_episode(original, candidate(17.5))
    assert not same_episode(original, candidate(16.5))
    last = candidate(22, pts=(0, 20, 22))
    assert admissible_trigger_ranges(last) == ((21, 23),)
    assert same_episode(last, candidate(22.5))
    assert not same_episode(last, candidate(23.5))


def test_same_class_is_required_unless_reclassification_is_explicit():
    original = candidate()
    other_class = candidate(rule_id='R22')
    assert not same_episode(original, other_class)
    assert same_episode(original, other_class, ignore_rule=True)
    assert not same_episode(original, candidate(23, rule_id='R22'), ignore_rule=True)


def test_source_start_clamps_extrapolation_at_zero_and_keeps_original_pts():
    original = candidate(0, pts=(0, .4, 9))
    before = original.context_frames
    assert admissible_trigger_ranges(original) == ((0, .2),)
    assert same_episode(original, candidate(.2))
    assert original.context_frames is before
    assert [f.timestamp for f in original.context_frames] == [0, .4, 9]


def test_constituent_ranges_do_not_confirm_a_trigger_inside_only_the_merged_envelope():
    left = candidate(2, 4, 2, pts=(0, 2, 4, 6))
    right = candidate(18, 20, 18, pts=(16, 18, 20, 22))
    merged = candidate(2, 20, 2, pts=(0, 2, 4, 6, 16, 18, 20, 22))
    assert admissible_trigger_ranges(merged, constituents=(left, right)) == ((1, 5), (17, 21))
    assert not same_episode(merged, candidate(10), constituents=(left, right))
    assert same_episode(merged, candidate(19), constituents=(left, right))
    # Without provenance, the existing Candidate cannot reveal this distinction.
    assert same_episode(merged, candidate(10))


def test_overlapping_constituent_ranges_are_unioned_without_changing_input():
    left = candidate(2, 4, 2, pts=(0, 2, 4, 6))
    right = candidate(4, 6, 4, pts=(2, 4, 6, 8))
    merged = candidate(2, 6, 2, pts=(0, 2, 4, 6, 8))
    assert admissible_trigger_ranges(merged, constituents=(left, right)) == ((1, 7),)
    assert (left.start, right.start, merged.start) == (2, 4, 2)


@pytest.mark.parametrize('pts', [(), (5,), (4, 5, 5, 6), (6, 5, 4), (0, float('nan'), 5), (-1, 5)])
def test_invalid_screening_context_is_rejected_without_fallback_to_max_gap(pts):
    with pytest.raises(ValueError, match='Screening context'):
        admissible_trigger_ranges(candidate(5, pts=pts))


def test_event_boundaries_must_come_from_screening_pts():
    with pytest.raises(ValueError, match='boundaries'):
        admissible_trigger_ranges(candidate(5, pts=(0, 4, 6, 23)))


@pytest.mark.parametrize('change', [dict(start=-1), dict(end=4), dict(peak=6),
                                   dict(peak=float('inf')), dict(start=True)])
def test_malformed_original_or_refined_event_cannot_be_confirmed(change):
    invalid = replace(candidate(), **change)
    with pytest.raises(ValueError, match='event interval'):
        admissible_trigger_ranges(invalid)
    with pytest.raises(ValueError, match='event interval'):
        same_episode(candidate(), invalid)


def test_empty_wrong_class_or_outside_constituents_are_rejected():
    original = candidate()
    for constituents in ((), (candidate(rule_id='R22'),), (candidate(23),)):
        with pytest.raises(ValueError, match='constituent'):
            admissible_trigger_ranges(original, constituents=constituents)
