from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from app.analyzer import AnalysisContext, LocalAnalysisResult
from app.local_analyzer import AnalyzerConfig, QwenLocalAnalyzer, build_analyzer
from app.ml.model import Event, WindowResult
from app.ml.rules import RULES
from app.ml.video import Frame, Video, VideoAnalysisError, select_frames
from ml_tools import evaluate, motion_sampling


class EmptyModel:
    def __init__(self):
        self.calls = []

    def infer(self, frames, timeout, *, refine=False, rule_ids=None):
        self.calls.append((list(frames), timeout, refine))
        return WindowResult(assessable=True, events=[])


@pytest.fixture
def decoded_source(monkeypatch, tmp_path):
    video = Video(12, tuple(Frame(index / 2, b'synthetic') for index in range(24)), 24)
    monkeypatch.setattr('app.local_analyzer.decode_video', lambda *args, **kwargs: video)
    context = AnalysisContext('synthetic', tmp_path / 'synthetic.mov', 12, ('R22',))
    return video, context


def test_selector_changes_only_local_screening_and_keeps_original_deadline(decoded_source):
    video, context = decoded_source
    selected = []

    def selector(source, start, end, count, *, deadline, audit):
        selected.append((start, end, deadline))
        audit['windows'] = audit.get('windows', 0) + 1
        available = [frame for frame in source.frames if start <= frame.timestamp <= end]
        return [available[0], available[1], available[-2], available[-1]]

    model = EmptyModel()
    _, metrics = QwenLocalAnalyzer(
        AnalyzerConfig(frames_per_window=4, window_sec=6, stride_sec=6),
        model=model, local_frame_selector=selector,
    ).analyze_with_metrics(context)

    assert [(start, end) for start, end, _ in selected] == [(0, 6), (6, 12)]
    assert selected[0][2] == selected[1][2]
    assert model.calls[0][0] == select_frames(video, 0, 12, 4)
    assert [frame.timestamp for frame in model.calls[1][0]] == [0, 0.5, 5.5, 6]
    assert [frame.timestamp for frame in model.calls[2][0]] == [6, 6.5, 11, 11.5]
    assert len(model.calls) == metrics['screening_passes'] == 3
    assert metrics['local_frame_selection'] == {'windows': 2}


@pytest.mark.parametrize('confirmation', ['confirm', 'reject', 'different_episode'])
def test_selected_candidate_still_requires_original_same_episode_refinement(decoded_source, confirmation):
    video, context = decoded_source
    selections = []

    def selector(source, start, end, count, *, deadline, audit):
        selections.append((start, end))
        return [source.frames[index] for index in (0, 1, 22, 23)]

    class CandidateModel(EmptyModel):
        def infer(self, frames, timeout, *, refine=False, rule_ids=None):
            super().infer(frames, timeout, refine=refine, rule_ids=rule_ids)
            if refine and confirmation == 'reject':
                return WindowResult(assessable=True, events=[])
            point = next(index for index, frame in enumerate(frames) if frame.timestamp == 0.5)
            if refine and confirmation == 'different_episode':
                point = len(frames) - 1
            return WindowResult(assessable=True, events=[Event(
                rule_id='R22', start_frame=point, peak_frame=point, end_frame=point,
                conditions=list(RULES['R22'].criteria), observation='Synthetic source-timestamp event.')])

    model = CandidateModel()
    result, metrics = QwenLocalAnalyzer(
        AnalyzerConfig(frames_per_window=4), model=model, local_frame_selector=selector,
    ).analyze_with_metrics(context)
    assert selections == [(0, 12)]
    assert [call[2] for call in model.calls] == [False, True]
    confirmed = confirmation == 'confirm'
    assert [event.timecode_sec for event in result.violations] == ([0.5] if confirmed else [])
    assert metrics['refinement_confirmed_candidates'] == int(confirmed)
    assert metrics['refinement_rejected_candidates'] == int(not confirmed)
    assert all(any(frame is original for original in video.frames) for frame in model.calls[1][0])
    assert all(any(frame is original for original in model.calls[1][0]) for frame in model.calls[0][0])


def test_slow_selector_cannot_extend_original_analysis_budget(decoded_source, monkeypatch):
    _, context = decoded_source
    clock = [100.0]
    monkeypatch.setattr('app.local_analyzer.time.monotonic', lambda: clock[0])

    def selector(source, start, end, count, *, deadline, audit):
        assert deadline == 105
        clock[0] = 106
        return select_frames(source, start, end, count)

    model = EmptyModel()
    with pytest.raises(VideoAnalysisError, match='time budget exhausted'):
        QwenLocalAnalyzer(AnalyzerConfig(budget_sec=5), model=model,
                          local_frame_selector=selector).analyze(context)
    assert model.calls == []


@pytest.mark.parametrize('count', [0, 1])
def test_selector_requires_a_temporal_pair_before_any_model_request(decoded_source, count):
    _, context = decoded_source

    def selector(source, start, end, limit, *, deadline, audit):
        return list(source.frames[:count])

    model = EmptyModel()
    with pytest.raises(VideoAnalysisError, match='Invalid local frame selection'):
        QwenLocalAnalyzer(AnalyzerConfig(frames_per_window=4), model=model,
                          local_frame_selector=selector).analyze(context)
    assert model.calls == []


@pytest.mark.parametrize('bad_selection', ['over_cap', 'duplicate', 'reverse', 'modified_frame', 'outside_window'])
def test_selector_cannot_send_invalid_or_modified_frames_to_model(decoded_source, bad_selection):
    _, context = decoded_source

    def selector(source, start, end, count, *, deadline, audit):
        if bad_selection == 'over_cap':
            return list(source.frames[:count + 1])
        if bad_selection == 'duplicate':
            return [source.frames[0], source.frames[0]]
        if bad_selection == 'reverse':
            return [source.frames[1], source.frames[0]]
        if bad_selection == 'modified_frame':
            return [source.frames[0], Frame(0.5, b'not-original')]
        return [source.frames[0], source.frames[-1]]

    model = EmptyModel()
    with pytest.raises(VideoAnalysisError, match='Invalid local frame selection'):
        QwenLocalAnalyzer(AnalyzerConfig(frames_per_window=4, window_sec=6, stride_sec=6),
                          model=model, local_frame_selector=selector).analyze(context)
    # The optional whole-video overview remains unchanged and precedes the bad local selection.
    assert len(model.calls) == 1


def test_factory_keeps_uniform_default_even_if_unrecognized_environment_flag_is_set(monkeypatch):
    monkeypatch.setenv('APP_ML_MOTION_SAMPLING', 'true')
    assert build_analyzer().local_frame_selector is None


def test_evaluation_cli_wires_motion_selection_and_records_exact_code_hash(tmp_path, monkeypatch,
                                                                         isolated_evaluation_lock):
    source = tmp_path / 'synthetic.mov'
    source.write_bytes(b'synthetic-CLI-fixture')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'records': [{
        'id': 'synthetic', 'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'label': 'no_violation', 'point_sec': None, 'duration_sec': 12, 'split': 'dev',
    }]}))
    output = tmp_path / 'evaluation'
    selectors = []

    class Analyzer:
        def __init__(self, config, *, local_frame_selector):
            selectors.append(local_frame_selector)

        def analyze_with_metrics(self, context):
            return LocalAnalysisResult(verdict='no_violation', violations=[],
                                       checked_rule_ids=list(context.enabled_rule_ids)), {
                'elapsed_sec': 0.1, 'local_frame_selection': {'windows': 1, 'changed_frames': 2},
            }

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(evaluate.MemorySampler, 'run', lambda self: self.stop.wait(2))
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--manifest', str(manifest), '--output', str(output),
                                     '--mode', 'dev', '--motion-sampling'])
    evaluate.main()
    assert selectors == [motion_sampling.select_motion_frames]
    report = json.loads((output / 'metrics.json').read_text())
    assert report['provenance']['motion_sampling'] is True
    assert report['provenance']['sampling_policy'] == motion_sampling.SAMPLING_POLICY
    assert report['provenance']['component_sha256']['ml_tools/motion_sampling.py'] == hashlib.sha256(
        Path(motion_sampling.__file__).read_bytes()).hexdigest()
    assert report['rows'][0]['local_frame_selection'] == {'windows': 1, 'changed_frames': 2}
