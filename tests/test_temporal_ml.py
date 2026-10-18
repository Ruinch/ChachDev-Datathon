from __future__ import annotations

import io
import json
import random
import subprocess

import pytest

av = pytest.importorskip('av')
Image = pytest.importorskip('PIL.Image')

from app.ml.media_input import encode_sampled_clip, temporal_content
from app.ml.model import LoopbackModel
from ml_tools.observation import Episode, map_episode
from app.ml.video import Frame, VideoAnalysisError


def samples():
    result = []
    for index, timestamp in enumerate([0.25, 3.5, 11.2, 42.1]):
        stream = io.BytesIO()
        Image.new('RGB', (129, 97), (index*60, 30, 60)).save(stream, format='JPEG')
        result.append(Frame(timestamp, stream.getvalue()))
    return result


def test_temporal_input_is_silent_ordered_and_keeps_source_mapping():
    frames = samples()
    original = [(f.timestamp, f.jpeg) for f in frames]
    raw = encode_sampled_clip(frames)
    with av.open(io.BytesIO(raw)) as container:
        assert len(container.streams.audio) == 0
        decoded = list(container.decode(video=0))
        assert len(decoded) == len(frames)
        assert [float(f.pts*f.time_base) for f in decoded] == [0, 1, 2, 3]
        assert [(f.width, f.height) for f in decoded] == [(130, 98)]*4
        red = [f.to_image().getpixel((32, 32))[0] for f in decoded]
        assert all(right > left+35 for left, right in zip(red, red[1:]))
    parts = temporal_content(frames)
    assert '3=42.100s' in parts[0]['text']
    assert parts[1]['type'] == 'input_video'
    assert [(f.timestamp, f.jpeg) for f in frames] == original


def test_temporal_clip_can_be_decoded_through_nonseekable_pipe():
    imageio = pytest.importorskip('imageio_ffmpeg')
    # A tiny MP4 can accidentally fit in FFmpeg's probe buffer and hide a trailing
    # moov/seek bug. Use a full window with enough encoded bytes to exceed it.
    rng = random.Random(26)
    frames = []
    for index in range(24):
        stream = io.BytesIO()
        Image.frombytes('RGB', (256, 256), rng.randbytes(256*256*3)).save(stream, format='JPEG')
        frames.append(Frame(float(index), stream.getvalue()))
    completed = subprocess.run([imageio.get_ffmpeg_exe(), '-v', 'error', '-i', 'pipe:0',
        '-map', '0:v:0', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
        input=encode_sampled_clip(frames), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert completed.returncode == 0
    assert len(completed.stdout) == 24*256*256*3


def test_temporal_request_stays_inline_and_frame_indices_are_validated():
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'content': '{"assessable": true, "events": []}'}}]}).encode()
    class Opener:
        def open(self, request, **kwargs):
            captured.append(json.loads(request.data))
            assert request.full_url.startswith('http://127.0.0.1:8091/')
            return Response()
    model = LoopbackModel(input_mode='video')
    model.opener = Opener()
    result = model.infer(samples(), 5)
    assert result.assessable and result.events == []
    assert captured[0]['cache_prompt'] is False
    parts = captured[0]['messages'][1]['content']
    videos = [p for p in parts if p['type'] == 'input_video']
    assert len(videos) == 1
    assert videos[0]['input_video']['data'].startswith('data:video/mp4;base64,')
    assert not any(p['type'] in {'input_audio', 'audio_url'} for p in parts)
    assert captured[0]['response_format']['json_schema']['schema']['$defs']['Event']['properties']['peak_frame']['maximum'] == 3


def test_expired_budget_makes_no_model_request():
    model = LoopbackModel(input_mode='video')
    class Never:
        def open(self, *args, **kwargs):
            pytest.fail('Expired request must not reach the model')
    model.opener = Never()
    with pytest.raises(VideoAnalysisError, match='budget'):
        model.infer(samples(), 0)


def departure(**changes):
    fields = dict(start_frame=0, peak_frame=2, end_frame=3, action='cashier_departs',
        role_grounded=True, whole_relevant_area_visible=True, operation='customer_cash_deposit',
        deposit_handover_visible=True, deposit_finished_before_action=True, deposit_still_in_progress=False,
        same_cash_continuously_visible=True, object_on_desk='banknotes',
        cash_counted_after_customer_departure=False, next_customer_service_visible=False,
        cashier_reaches_storage=False, observation='Synthetic visible cash-handling sequence.')
    fields.update(changes)
    return Episode(**fields)


@pytest.mark.parametrize('change', [dict(role_grounded=False), dict(whole_relevant_area_visible=False),
    dict(operation='unknown'), dict(deposit_handover_visible=False), dict(object_on_desk='documents'),
    dict(same_cash_continuously_visible=False), dict(cashier_reaches_storage=True),
    dict(deposit_still_in_progress=True)])
def test_missing_or_contradictory_facts_reject_cash_left_event(change):
    assert map_episode(departure(**change), ('R20-C',)) == []


def test_observation_mapping_keeps_role_and_operation_distinctions():
    assert [e.rule_id for e in map_episode(departure(), ('R20-C', 'R22'))] == ['R20-C']
    ongoing = departure(deposit_finished_before_action=False, deposit_still_in_progress=True)
    assert [e.rule_id for e in map_episode(ongoing, ('R20-C', 'R22'))] == ['R22']
    customer = ongoing.model_copy(update={'action':'customer_departs', 'cash_counted_after_customer_departure': True})
    assert [e.rule_id for e in map_episode(customer, ('R22', 'R20-A'))] == ['R20-A']
    assert map_episode(departure(), ('R20-A',)) == []
