from app.local_analyzer import Candidate, merge_events, refinement_frames, same_candidate
from app.ml.video import Frame, Video


def candidate(start=10, end=12, peak=11, **kwargs):
    return Candidate('R20-A', start, end, peak, 'Synthetic temporal event.', **kwargs)


def test_refinement_preserves_screening_role_and_handover_frames():
    video = Video(24, tuple(Frame(i/2, bytes([i])) for i in range(48)), 720)
    context = tuple(video.frames[i] for i in [0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47])
    event = candidate(context_start=0, context_end=23.5, context_frames=context)
    result = refinement_frames(video, event, 16)
    assert set(context).issubset(result)
    assert len(result) == 16 and result == sorted(result, key=lambda f: f.timestamp)
    assert all(8 <= f.timestamp <= 14 for f in result if f not in context)


def test_merged_context_is_bounded_without_losing_event_and_outer_anchors():
    frames = tuple(Frame(float(i), b'synthetic') for i in range(80))
    left = candidate(context_start=0, context_end=39, context_frames=frames[:40])
    right = candidate(context_start=30, context_end=79, context_frames=frames[30:])
    merged = merge_events([left, right])[0]
    result = refinement_frames(Video(80, frames, 2400), merged, 28)
    assert len(result) == 28
    assert {0, 10, 11, 12, 79}.issubset({f.timestamp for f in result})


def test_refinement_shift_is_bounded_by_screening_resolution_and_rule():
    original = candidate(start=10, end=10, peak=10, sample_tolerance=1)
    assert same_candidate(original, candidate(start=10.5, end=10.5, peak=10.5))
    assert not same_candidate(original, candidate(start=14, end=15, peak=14))
    assert not same_candidate(original, Candidate('R22', 10, 10, 10, 'Unrelated rule'))
