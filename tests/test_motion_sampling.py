from __future__ import annotations

import io
import time

import pytest
from PIL import Image, ImageDraw

from app.ml.video import Frame, Video, VideoAnalysisError, select_frames
from ml_tools.motion_sampling import select_motion_frames


def scene(*, pulse=False, brightness=50, flip=False):
    image = Image.new('RGB', (96, 96), (brightness,) * 3)
    if pulse:
        ImageDraw.Draw(image).rectangle((3, 3, 20, 20), fill=(230,) * 3)
    if flip:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    stream = io.BytesIO()
    image.save(stream, format='JPEG', quality=95)
    return stream.getvalue()


def sequence(*, frames=25, pulses=(12,), flip=False, times=None):
    timestamps = times if times is not None else [index / 2 for index in range(frames)]
    values = tuple(Frame(timestamp, scene(pulse=index in pulses, flip=flip))
                   for index, timestamp in enumerate(timestamps))
    return Video(timestamps[-1] + 0.5, values, len(values))


def select(video, *, count=8, start=0, end=None, **kwargs):
    return select_motion_frames(video, start, video.duration if end is None else end,
                                count, deadline=time.monotonic() + 10, **kwargs)


def test_brief_local_change_adds_before_after_pair_missing_from_uniform():
    video = sequence()
    uniform = select_frames(video, 0, video.duration, 8)
    assert 6.0 not in [frame.timestamp for frame in uniform]
    audit = {}
    result = select(video, audit=audit)
    pts = [frame.timestamp for frame in result]
    assert 5.5 in pts and 6.0 in pts
    assert {0.0, 4.0, 8.0, 12.0} <= set(pts)  # Uniform half-budget anchors.
    assert len(pts) == len(set(pts)) == 8
    assert pts == sorted(pts)
    assert audit['changed_windows'] == 1
    assert audit['selected_pairs'] == 1  # Adjacent on/off changes are suppressed.
    assert all(isinstance(value, int) for value in audit.values())


def test_selection_keeps_original_objects_and_nonuniform_source_pts():
    timestamps = [index / 2 + (0.12 if index % 3 == 0 else 0.03)
                  for index in range(25)]
    video = sequence(times=timestamps)
    original = [(id(frame), frame.timestamp, frame.jpeg) for frame in video.frames]
    result = select(video, start=timestamps[3], end=timestamps[22], count=8)
    assert result[0] is video.frames[3] and result[-1] is video.frames[22]
    assert all(any(frame is candidate for candidate in video.frames) for frame in result)
    assert all(timestamps[3] <= frame.timestamp <= timestamps[22] for frame in result)
    assert original == [(id(frame), frame.timestamp, frame.jpeg) for frame in video.frames]


@pytest.mark.parametrize('pulses', [(), (5, 17)])
def test_horizontal_view_mirroring_does_not_change_selected_times(pulses):
    left = sequence(pulses=pulses)
    right = sequence(pulses=pulses, flip=True)
    assert [frame.timestamp for frame in select(left)] == [frame.timestamp for frame in select(right)]


def test_static_scene_falls_back_to_exact_uniform_selection():
    video = sequence(pulses=())
    audit = {}
    assert select(video, audit=audit) == select_frames(video, 0, video.duration, 8)
    assert audit['selected_pairs'] == 0 and audit['fallback_windows'] == 1


def test_uniform_lighting_flash_does_not_steal_motion_slots():
    frames = tuple(Frame(index / 2, scene(brightness=200 if index == 12 else 50))
                   for index in range(25))
    video = Video(12.5, frames, 25)
    audit = {}
    assert select(video, audit=audit) == select_frames(video, 0, video.duration, 8)
    assert audit['ranked_transitions'] == 0


def test_regular_whole_image_pan_is_suppressed_without_claiming_general_motion_compensation():
    frames = []
    for index in range(25):
        image = Image.new('RGB', (96, 96))
        draw = ImageDraw.Draw(image)
        for x in range(96):
            value = 220 if ((x + index) % 8) < 4 else 40
            draw.line((x, 0, x, 95), fill=(value,) * 3)
        stream = io.BytesIO()
        image.save(stream, format='JPEG', quality=95)
        frames.append(Frame(index / 2, stream.getvalue()))
    video = Video(12.5, tuple(frames), 25)
    audit = {}
    assert select(video, audit=audit) == select_frames(video, 0, video.duration, 8)
    assert audit['selected_pairs'] == 0


@pytest.mark.parametrize('count', [2, 3, 4, 5, 8, 13, 24, 32])
def test_various_budgets_never_exceed_cap_or_drop_endpoints(count):
    video = sequence(frames=41, pulses=(3, 8, 14, 20, 27, 33, 37))
    result = select(video, count=count)
    assert len(result) == count
    assert result[0] is video.frames[0] and result[-1] is video.frames[-1]
    assert len({frame.timestamp for frame in result}) == count


def test_all_fit_does_not_decode_frames(monkeypatch):
    video = sequence(frames=6)
    monkeypatch.setattr('ml_tools.motion_sampling._signature',
                        lambda frame: pytest.fail('All-fit windows must not decode motion inputs'))
    assert select(video, count=8) == list(video.frames)


def test_original_deadline_is_enforced_inside_image_loop(monkeypatch):
    from ml_tools import motion_sampling
    video = sequence()
    clock = [100.0]
    calls = []
    original = motion_sampling._signature
    monkeypatch.setattr('app.ml.video.time.monotonic', lambda: clock[0])

    def slow_signature(frame):
        calls.append(frame.timestamp)
        clock[0] += 2
        return original(frame)

    monkeypatch.setattr(motion_sampling, '_signature', slow_signature)
    with pytest.raises(VideoAnalysisError, match='time budget exhausted'):
        select_motion_frames(video, 0, video.duration, 8, deadline=103.0)
    assert len(calls) == 2


def test_expired_budget_does_not_even_decode_first_image(monkeypatch):
    monkeypatch.setattr('ml_tools.motion_sampling._signature',
                        lambda frame: pytest.fail('Expired budget must not decode inputs'))
    with pytest.raises(VideoAnalysisError, match='time budget exhausted'):
        select_motion_frames(sequence(), 0, 12, 8, deadline=time.monotonic() - 1)


def test_bad_image_error_does_not_echo_private_bytes():
    video = Video(12.5, tuple(Frame(index / 2, b'private-invalid-input')
                             for index in range(25)), 25)
    with pytest.raises(VideoAnalysisError) as caught:
        select(video)
    assert str(caught.value) == 'Cannot decode local motion-sampling input'


@pytest.mark.parametrize('changes', [dict(count=True), dict(count=1), dict(count=4.5),
                                    dict(start=5, end=2), dict(deadline=float('inf'))])
def test_invalid_selection_settings_are_rejected(changes):
    kwargs = dict(start=0, end=12, count=8, deadline=time.monotonic() + 10)
    kwargs.update(changes)
    with pytest.raises(ValueError, match='Invalid motion-sampling'):
        select_motion_frames(sequence(), **kwargs)
