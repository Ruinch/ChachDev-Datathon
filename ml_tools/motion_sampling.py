"""Research-only temporal selection from local, already decoded frames.

This is attention allocation, not an action/person/cash detector. Half the input
budget remains uniformly spaced; the remaining slots prefer before/after pairs
around localized visual changes. The original Frame objects and PTS are retained.
Call this only for local windows: the production global overview remains uniform.
No model, production factory, or environment flag is changed by this module.
"""
from __future__ import annotations

import io
import math
from collections.abc import MutableMapping
from statistics import median

from PIL import Image, ImageChops, ImageStat

from app.ml.video import Frame, Video, VideoAnalysisError, check_deadline, select_frames


SAMPLING_POLICY = 'uniform-half-local-change-pairs-v1'
_SIDE = 96
_GRID = 4
_MIN_CHANGE = 1.0
_PAIR_SPACING_SEC = 1.0


def _signature(frame: Frame) -> Image.Image:
    try:
        with Image.open(io.BytesIO(frame.jpeg)) as image:
            return image.convert('L').resize((_SIDE, _SIDE), Image.Resampling.BILINEAR)
    except (OSError, ValueError):
        raise VideoAnalysisError('Cannot decode local motion-sampling input') from None


def _localized_change(previous: Image.Image, current: Image.Image) -> float:
    """Suppress global brightness changes; rank the strongest local tile change.

    A camera pan with nonuniform texture can still score highly. Therefore this
    signal never establishes a role or a violation, and uniform anchors remain.
    """
    difference = ImageChops.difference(previous, current)
    step = _SIDE // _GRID
    values = [ImageStat.Stat(difference.crop((x, y, x + step, y + step))).mean[0]
              for y in range(0, _SIDE, step) for x in range(0, _SIDE, step)]
    return max(values) - median(values)


def _increment(audit: MutableMapping[str, int] | None, **counts: int) -> None:
    if audit is not None:
        for key, value in counts.items():
            audit[key] = audit.get(key, 0) + value


def select_motion_frames(video: Video, start: float, end: float, count: int, *,
                         deadline: float,
                         audit: MutableMapping[str, int] | None = None) -> list[Frame]:
    """Select at most count original frames within one local temporal window.

    Use the analyzer's existing absolute deadline. No extra model calls or time
    budget are introduced. For static/all-fit windows the result is exactly the
    existing uniform selector's result. Audit contains aggregate counts only.
    """
    if (isinstance(count, bool) or not isinstance(count, int) or count < 2
            or not all(math.isfinite(value) for value in (start, end, deadline))
            or end < start):
        raise ValueError('Invalid motion-sampling window, count, or deadline')
    check_deadline(deadline)
    available = [frame for frame in video.frames if start <= frame.timestamp <= end]
    uniform = select_frames(video, start, end, count)
    _increment(audit, windows=1)
    if len(available) <= count or count < 4:
        _increment(audit, fallback_windows=1, selected_frames=len(uniform))
        return uniform

    anchor_count = max(2, count // 2)
    anchor_indices = {round(index * (len(available) - 1) / (anchor_count - 1))
                      for index in range(anchor_count)}
    chosen = set(anchor_indices)
    previous = None
    scored = []
    for index, frame in enumerate(available):
        check_deadline(deadline)
        current = _signature(frame)
        if previous is not None:
            score = _localized_change(previous, current)
            if score >= _MIN_CHANGE:
                scored.append((score, index))
        previous = current
    check_deadline(deadline)

    selected_transitions: list[float] = []
    for _, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        check_deadline(deadline)
        timestamp = available[index].timestamp
        if any(abs(timestamp - previous_time) < _PAIR_SPACING_SEC
               for previous_time in selected_transitions):
            continue
        pair = {index - 1, index}
        added = pair - chosen
        # Retain both sides of a transition; never add a dangling partial pair.
        if not added or len(chosen) + len(added) > count:
            continue
        chosen.update(pair)
        selected_transitions.append(timestamp)
        if len(chosen) == count:
            break

    if not selected_transitions:
        selected = uniform
    else:
        uniform_indices = [round(index * (len(available) - 1) / (count - 1))
                           for index in range(count)]
        for index in uniform_indices:
            if len(chosen) == count:
                break
            chosen.add(index)
        selected = [available[index] for index in sorted(chosen)]
    check_deadline(deadline)
    uniform_pts = {frame.timestamp for frame in uniform}
    changed = sum(frame.timestamp not in uniform_pts for frame in selected)
    _increment(audit, decoded_signatures=len(available), ranked_transitions=len(scored),
               selected_pairs=len(selected_transitions), uniform_anchor_frames=anchor_count,
               selected_frames=len(selected), changed_frames=changed,
               changed_windows=int(changed > 0), fallback_windows=int(not selected_transitions))
    return selected
