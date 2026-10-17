from __future__ import annotations

import json
import threading
import time
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

av = pytest.importorskip('av', reason='Install .[ml,test] to run local ML tests')
Image = pytest.importorskip('PIL.Image')

from app.analyzer import AnalysisContext, InsufficientEvidenceError
from app.rules import load_rule_catalog
from pathlib import Path
from app.evidence import materialize_result
from app.local_analyzer import AnalyzerConfig, Candidate, QwenLocalAnalyzer, merge_events
from app.ml.model import Event, LoopbackModel, WindowResult, validate_endpoint
from app.ml.rules import RULES
from app.ml.video import Frame, Video, VideoAnalysisError, decode_video, windows
from ml_tools.dataset import assign_splits, canonical_id, parse_timestamp
from ml_tools.evaluate import summarize
from ml_tools.annotations import match_events, validate_annotations


@pytest.mark.parametrize('endpoint', ['https://127.0.0.1:8091', 'http://example.com:80', 'http://localhost:8091',
    'http://127.0.0.1.evil.test:80', 'http://user@127.0.0.1:80', 'http://127.0.0.1:80?x=1',
    'http://127.0.0.1:80/remote', 'http://127.0.0.1'])
def test_remote_model_endpoints_rejected(endpoint):
    with pytest.raises(ValueError):
        validate_endpoint(endpoint)


def test_tail_coverage_and_no_gap():
    spans = windows(142.533, 18, 12)
    assert spans[0][0] == 0 and spans[-1][1] == 142.533
    assert all(right[0] <= left[1] for left, right in zip(spans, spans[1:]))
    assert windows(11.63, 18, 12) == [(0, 11.63)]
    with pytest.raises(ValueError):
        windows(10, 4, 5)


def test_dedup_preserves_different_episodes_and_classes():
    events = [Candidate('R22', 1, 4, 2, 'first'), Candidate('R22', 3, 5, 4, 'same'),
              Candidate('R22', 8, 9, 8, 'second'), Candidate('R20-A', 1, 4, 2, 'other')]
    result = merge_events(events)
    assert len(result) == 3
    assert {(e.rule_id, e.start, e.end) for e in result} == {('R22', 1, 5), ('R22', 8, 9), ('R20-A', 1, 4)}


def test_parse_source_labels():
    assert canonical_id('Х-1.mov') == canonical_id('X-1')
    assert parse_timestamp('1минута 8 секунда') == 68
    assert parse_timestamp('00:05') == 5
    assert parse_timestamp(None) is None
    with pytest.raises(ValueError):
        parse_timestamp('NaN')


def test_split_quarantines_conflict_and_keeps_duplicates_together():
    rows = [dict(sha256=str(i), label='violation' if i % 2 else 'no_violation', point_sec=5 if i % 2 else None, duration_sec=10+i) for i in range(20)]
    rows += [dict(rows[3], point_sec=6), dict(rows[4])]
    assign_splits(rows)
    assert all(r['split'] == 'quarantine' for r in rows if r['sha256'] == '3')
    assert len({r['split'] for r in rows if r['sha256'] == '4'}) == 1
    assert rows[0]['split'] == 'dev' and rows[19]['split'] == 'dev'


def test_failed_predictions_never_count_as_true_negatives():
    rows = [dict(status='failed', label='no_violation', elapsed_sec=1),
            dict(status='done', label='violation', verdict='no_violation', elapsed_sec=2, event_times=[])]
    metrics = summarize(rows)
    assert metrics['failed'] == 1 and metrics['fn'] == 1 and metrics['tn'] == 0
    assert metrics['coverage'] == 0.5 and metrics['correct_over_all'] == 0


def test_event_matching_is_one_to_one_and_rule_specific():
    truth = [dict(rule_id='R22', start_sec=3, end_sec=5), dict(rule_id='R22', start_sec=10, end_sec=12)]
    pred = [dict(rule_id='R22', timecode_sec=4), dict(rule_id='R22', timecode_sec=4.5), dict(rule_id='R20-A', timecode_sec=11)]
    assert match_events(truth, pred) == dict(tp=1, fp=2, fn=1)


def test_holdout_group_leakage_is_rejected():
    manifest = {'records': [dict(id='a', sha256='hash-a', split='dev', duration_sec=10),
                            dict(id='b', sha256='hash-b', split='holdout', duration_sec=10)]}
    annotations = {r['id']: dict(sha256=r['sha256'], reviewed=True, source_group='same-session',
                    verdict='no_violation', events=[]) for r in manifest['records']}
    with pytest.raises(ValueError, match='overlap'):
        validate_annotations(manifest, annotations, require_all=True)


def synthetic_video(path):
    with av.open(str(path), 'w', format='mov') as out:
        stream = out.add_stream('mpeg4', rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 96, 'yuv420p'
        for i in range(30):
            frame = av.VideoFrame.from_image(Image.new('RGB', (64, 96), (i*8, 20, 200-i*5)))
            frame.pts, frame.time_base = i, Fraction(1, 10)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


def test_decode_and_actual_source_evidence(tmp_path, synthetic_ml_catalog):
    import imageio_ffmpeg
    source = tmp_path / 'synthetic.mov'
    synthetic_video(source)
    video = decode_video(source, fps=2, max_side=128, deadline=time.monotonic()+10)
    assert video.decoded_frames == 30
    assert video.frames[0].timestamp == 0
    assert video.frames[-1].timestamp == pytest.approx(2.9)
    assert all(f.jpeg.startswith(b'\xff\xd8') for f in video.frames)
    class FixtureModel:
        def infer(self, frames, timeout, **kwargs):
            return WindowResult(assessable=True, events=[Event(rule_id='R22', start_frame=0, end_frame=len(frames)-1,
                peak_frame=2, conditions=list(RULES['R22'].criteria), observation='Синтетический тест структуры события.')])
    result = QwenLocalAnalyzer(model=FixtureModel()).analyze(AnalysisContext('synthetic', source, 3.0, ('R22',)))
    response = materialize_result('synthetic', source, tmp_path/'evidence', result, imageio_ffmpeg.get_ffmpeg_exe(), 3.0, synthetic_ml_catalog)
    assert response.verdict == 'violation'
    assert 1 <= len(response.violations[0].evidence_frames) <= 3
    assert all(frame.image_base64.startswith('/9j/') for frame in response.violations[0].evidence_frames)


def test_incomplete_context_fails_closed(tmp_path):
    source = tmp_path/'test.mov'
    synthetic_video(source)
    class HiddenModel:
        def infer(self, *args, **kwargs):
            return WindowResult(assessable=False, events=[])
    with pytest.raises(InsufficientEvidenceError, match='visual context'):
        QwenLocalAnalyzer(model=HiddenModel()).analyze(AnalysisContext('test', source, 3.0, ('R22',)))


def test_decode_failure_never_calls_model(tmp_path):
    source = tmp_path/'corrupt.mov'
    source.write_bytes(b'not a video')
    class NeverModel:
        def infer(self, *args, **kwargs):
            pytest.fail('Model must not run for corrupt video')
    with pytest.raises(VideoAnalysisError):
        QwenLocalAnalyzer(model=NeverModel()).analyze(AnalysisContext('test', source, 3.0, ('R22',)))


def test_model_rejects_redirects_without_external_request(monkeypatch):
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header('Location', 'https://example.com/forbidden')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    try:
        model = LoopbackModel(f'http://127.0.0.1:{server.server_port}')
        with pytest.raises(VideoAnalysisError, match='redirects'):
            model.infer([Frame(0, b'jpeg'), Frame(1, b'jpeg')], timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(finish_reason='length'),
    lambda p: p['message'].update(content='not json'),
    lambda p: p['message'].update(content=json.dumps({'assessable': 'false', 'events': []})),
    lambda p: p['message'].update(content=json.dumps({'assessable': True, 'events': [{'rule_id':'R22', 'start_frame':0, 'end_frame':999, 'peak_frame':0, 'conditions':list(RULES['R22'].criteria), 'observation':'Test observation.'}]})),
])
def test_malformed_or_truncated_model_response_fails(mutate):
    choice = {'finish_reason': 'stop', 'message': {'content': '{"assessable":true,"events":[]}'}}
    mutate(choice)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return json.dumps({'choices': [choice]}).encode()
    class Opener:
        def open(self, *args, **kwargs): return Response()
    model = LoopbackModel()
    model.opener = Opener()
    with pytest.raises(VideoAnalysisError):
        model.infer([Frame(0, b'jpeg'), Frame(1, b'jpeg')], timeout=1)
