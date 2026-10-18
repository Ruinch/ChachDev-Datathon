"""Source-time check that refinement keeps the original triggering episode.

The service factory uses this matcher; the evaluation CLI enables it explicitly
with --strict-episode-matching to retain reproducible legacy comparisons. It reads
source timestamps only; it neither decodes images nor calls a model.

An interval-overlap check can be satisfied by a whole-context refined interval
whose triggering peak is unrelated to the screening event. Here the *peak* must
lie in the original event interval, padded by half the immediately neighboring
selected PTS spacing at each boundary. A distant sparse gap cannot increase that
padding. At a sampled window edge, the nearest inner spacing is extrapolated.

Candidate currently loses constituent provenance when events are merged. Callers
may explicitly supply the pre-merge candidates to preserve their range union.
Without them a broad merged interval remains broad; this helper cannot reconstruct
missing episode boundaries or prove two observations describe the same action.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.local_analyzer import Candidate


MATCHING_POLICY = 'screen-interval-neighbor-pts-trigger-v1'


def _validate_interval(event: Candidate) -> None:
    values = (event.start, event.peak, event.end)
    if (any(isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in values)
            or not 0 <= event.start <= event.peak <= event.end):
        raise ValueError('Invalid candidate event interval')


def _trigger_range(event: Candidate) -> tuple[float, float]:
    _validate_interval(event)
    timestamps = [frame.timestamp for frame in event.context_frames]
    if (len(timestamps) < 2
            or any(isinstance(t, bool) or not isinstance(t, (int, float))
                   or not math.isfinite(t) or t < 0 for t in timestamps)
            or any(left >= right for left, right in zip(timestamps, timestamps[1:]))):
        raise ValueError('Screening context needs at least two ordered source timestamps')
    if event.start not in timestamps or event.end not in timestamps:
        raise ValueError('Candidate boundaries must be selected screening timestamps')
    left = timestamps.index(event.start)
    right = timestamps.index(event.end)
    left_gap = (timestamps[left] - timestamps[left - 1] if left
                else timestamps[1] - timestamps[0])
    right_gap = (timestamps[right + 1] - timestamps[right] if right + 1 < len(timestamps)
                 else timestamps[-1] - timestamps[-2])
    return max(0., event.start - left_gap / 2), event.end + right_gap / 2


def admissible_trigger_ranges(
    original: Candidate, *, constituents: Sequence[Candidate] | None = None,
) -> tuple[tuple[float, float], ...]:
    """Return inclusive source-time ranges, without maximum-gap tolerances.

    Explicit constituents must have the same rule and lie within the merged
    original interval. Keep these objects before merging; the current Candidate
    itself has no constituent field. Inputs and source PTS are never modified.
    """
    _validate_interval(original)
    if constituents is None:
        return (_trigger_range(original),)
    if not constituents:
        raise ValueError('Explicit constituent candidates cannot be empty')
    ranges = []
    for event in constituents:
        _validate_interval(event)
        if (event.rule_id != original.rule_id
                or event.start < original.start or event.end > original.end):
            raise ValueError('Invalid constituent candidate scope')
        ranges.append(_trigger_range(event))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def same_episode(
    original: Candidate, refined: Candidate, *, ignore_rule: bool = False,
    constituents: Sequence[Candidate] | None = None,
) -> bool:
    """Match a refinement peak to screening, requiring the same class by default.

    ``ignore_rule=True`` is solely for an explicit reclassification experiment;
    the caller still must check enabled IDs and obtain another class confirmation.
    Temporal agreement is necessary, not independent factual confirmation.
    """
    _validate_interval(refined)
    ranges = admissible_trigger_ranges(original, constituents=constituents)
    return ((ignore_rule or original.rule_id == refined.rule_id)
            and any(start <= refined.peak <= end for start, end in ranges))
