from __future__ import annotations

from fractions import Fraction

import pytest

av = pytest.importorskip('av')
Image = pytest.importorskip('PIL.Image')

from app.analyzer import AnalysisContext, InsufficientEvidenceError
from app.local_analyzer import AnalyzerConfig, QwenLocalAnalyzer
from app.ml.model import Event, WindowResult
from app.ml.rules import RULES
from app.ml.video import VideoAnalysisError


def synthetic_context(tmp_path, *, seconds=4, rate=4):
    source = tmp_path / 'synthetic.mov'
    with av.open(str(source), 'w', format='mov') as output:
        stream = output.add_stream('mpeg4', rate=rate)
        stream.width, stream.height, stream.pix_fmt = 64, 96, 'yuv420p'
        for index in range(seconds * rate):
            frame = av.VideoFrame.from_image(Image.new('RGB', (64, 96), (index % 255, 30, 80)))
            frame.pts, frame.time_base = index, Fraction(1, rate)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return AnalysisContext('synthetic', source, seconds, ('R22',))


class EmptyModel:
    def __init__(self):
        self.calls = []

    def infer(self, frames, timeout, *, refine=False, rule_ids=None):
        self.calls.append(([frame.timestamp for frame in frames], refine, timeout))
        return WindowResult(assessable=True, events=[])


def test_complementary_pass_uses_omitted_original_frames_and_preserves_endpoints(tmp_path):
    model = EmptyModel()
    analyzer = QwenLocalAnalyzer(
        AnalyzerConfig(frames_per_window=4, complementary_sampling=True), model=model)
    result, metrics = analyzer.analyze_with_metrics(synthetic_context(tmp_path))

    assert result.violations == []
    assert [call[0] for call in model.calls] == [
        [0, 1.5, 2.5, 3.75],
        [0, 0.5, 3.5, 3.75],
    ]
    assert all(not call[1] for call in model.calls)
    assert metrics['complementary_passes'] == 1


@pytest.mark.parametrize('refinement', ['reject', 'confirm', 'different_episode'])
def test_new_candidate_requires_same_episode_refinement_and_stage_counts(tmp_path, refinement):
    class MissedFrameModel(EmptyModel):
        def infer(self, frames, timeout, *, refine=False, rule_ids=None):
            super().infer(frames, timeout, refine=refine, rule_ids=rule_ids)
            timestamps = [frame.timestamp for frame in frames]
            if 0.5 not in timestamps or (refine and refinement == 'reject'):
                return WindowResult(assessable=True, events=[])
            index = len(frames) - 1 if refine and refinement == 'different_episode' else timestamps.index(0.5)
            return WindowResult(assessable=True, events=[Event(
                rule_id='R22', start_frame=index, peak_frame=index, end_frame=index,
                conditions=list(RULES['R22'].criteria), observation='Synthetic omitted-frame candidate.')])

    model = MissedFrameModel()
    result, metrics = QwenLocalAnalyzer(
        AnalyzerConfig(frames_per_window=4, complementary_sampling=True), model=model
    ).analyze_with_metrics(synthetic_context(tmp_path))

    confirmed = refinement == 'confirm'
    assert [call[1] for call in model.calls] == [False, False, True]
    assert [event.timecode_sec for event in result.violations] == ([0.5] if confirmed else [])
    assert metrics['initial_candidates'] == 0
    assert metrics['complementary_candidates'] == 1
    assert metrics['complementary_candidate_counts'] == {'R22': 1}
    assert metrics['refinement_confirmed_candidates'] == int(confirmed)
    assert metrics['refinement_rejected_candidate_counts'] == ({} if confirmed else {'R22': 1})


@pytest.mark.parametrize('config', [
    AnalyzerConfig(frames_per_window=4),
    AnalyzerConfig(frames_per_window=12, complementary_sampling=True),
])
def test_default_off_and_all_frames_fit_make_no_redundant_request(tmp_path, config):
    model = EmptyModel()
    result, metrics = QwenLocalAnalyzer(config, model=model).analyze_with_metrics(synthetic_context(tmp_path))
    assert result.violations == []
    assert len(model.calls) == 1
    assert metrics['complementary_passes'] == 0


def test_global_overview_is_not_repeated_and_complementary_passes_stay_in_local_windows(tmp_path):
    model = EmptyModel()
    _, metrics = QwenLocalAnalyzer(
        AnalyzerConfig(frames_per_window=4, window_sec=4, stride_sec=4, complementary_sampling=True),
        model=model,
    ).analyze_with_metrics(synthetic_context(tmp_path, seconds=8))

    assert [(call[0][0], call[0][-1]) for call in model.calls] == [
        (0, 7.75), (0, 4), (4, 7.75), (0, 4), (4, 7.75),
    ]
    assert metrics['windows'] == 3
    assert metrics['screening_passes'] == 5
    assert metrics['complementary_passes'] == 2


def test_nonempty_initial_window_goes_directly_to_existing_refinement(tmp_path):
    class PositiveModel(EmptyModel):
        def infer(self, frames, timeout, *, refine=False, rule_ids=None):
            super().infer(frames, timeout, refine=refine, rule_ids=rule_ids)
            return WindowResult(assessable=True, events=[Event(
                rule_id='R22', start_frame=0, peak_frame=0, end_frame=0,
                conditions=list(RULES['R22'].criteria), observation='Synthetic initial-pass candidate.')])

    model = PositiveModel()
    result, metrics = QwenLocalAnalyzer(
        AnalyzerConfig(frames_per_window=4, complementary_sampling=True), model=model,
    ).analyze_with_metrics(synthetic_context(tmp_path))
    assert [call[1] for call in model.calls] == [False, True]
    assert len(result.violations) == 1
    assert metrics['initial_candidates'] == 1
    assert metrics['complementary_passes'] == 0
    assert metrics['refinement_confirmed_candidates'] == 1


@pytest.mark.parametrize('expire_on_call', [1, 2])
def test_complementary_pass_and_confirmation_share_original_deadline(tmp_path, monkeypatch, expire_on_call):
    context = synthetic_context(tmp_path)
    clock = [100.0]
    monkeypatch.setattr('app.local_analyzer.time.monotonic', lambda: clock[0])

    class SlowModel(EmptyModel):
        def infer(self, frames, timeout, *, refine=False, rule_ids=None):
            super().infer(frames, timeout, refine=refine, rule_ids=rule_ids)
            assert timeout == pytest.approx(5)
            if len(self.calls) == expire_on_call:
                clock[0] += 6
            if len(self.calls) == 2:
                return WindowResult(assessable=True, events=[Event(
                    rule_id='R22', start_frame=1, peak_frame=1, end_frame=1,
                    conditions=list(RULES['R22'].criteria), observation='Synthetic candidate after original deadline.')])
            return WindowResult(assessable=True, events=[])

    model = SlowModel()
    with pytest.raises(VideoAnalysisError, match='time budget exhausted'):
        QwenLocalAnalyzer(
            AnalyzerConfig(frames_per_window=4, complementary_sampling=True, budget_sec=5), model=model,
        ).analyze(context)
    assert len(model.calls) == expire_on_call
    assert not any(call[1] for call in model.calls)


def test_complementary_empty_answer_cannot_erase_unassessable_initial_pass(tmp_path):
    class IncompleteModel(EmptyModel):
        def infer(self, frames, timeout, *, refine=False, rule_ids=None):
            super().infer(frames, timeout, refine=refine, rule_ids=rule_ids)
            return WindowResult(assessable=len(self.calls) > 1, events=[])

    model = IncompleteModel()
    with pytest.raises(InsufficientEvidenceError, match='visual context'):
        QwenLocalAnalyzer(
            AnalyzerConfig(frames_per_window=4, complementary_sampling=True), model=model,
        ).analyze(synthetic_context(tmp_path))
    assert len(model.calls) == 2
