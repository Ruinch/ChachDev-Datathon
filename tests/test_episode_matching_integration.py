from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from app.analyzer import AnalysisContext, LocalAnalysisResult
from app.local_analyzer import AnalyzerConfig, QwenLocalAnalyzer, build_analyzer
from app.ml.model import Event, WindowResult
from app.ml import episode_matching as application_episode_matching
from app.ml.rules import RULES
from app.ml.video import Frame, Video
from ml_tools import episode_matching, evaluate


def event(frames, rule, start, end, peak):
    timestamps = [frame.timestamp for frame in frames]
    return Event(rule_id=rule, start_frame=timestamps.index(start), end_frame=timestamps.index(end),
                 peak_frame=timestamps.index(peak), conditions=list(RULES[rule].criteria),
                 observation='Synthetic temporal action for integration testing.')


@pytest.fixture
def source(monkeypatch, tmp_path):
    video = Video(24, tuple(Frame(float(i), b'synthetic') for i in range(24)), 24)
    monkeypatch.setattr('app.local_analyzer.decode_video', lambda *args, **kwargs: video)
    return AnalysisContext('synthetic', tmp_path / 'synthetic.mov', 24, ('R22', 'R20-A'))


class SequenceModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def infer(self, frames, timeout, *, refine=False, rule_ids=None):
        self.calls.append((refine, rule_ids, timeout, tuple(frames)))
        claims = self.responses.pop(0)
        return WindowResult(assessable=True, events=[event(frames, *claim) for claim in claims])


def analyzer(model, *, strict=True, reclassify=False, matcher=None):
    return QwenLocalAnalyzer(
        AnalyzerConfig(input_mode='video', frames_per_window=24, allow_reclassification=reclassify),
        model=model, candidate_matcher=matcher or (episode_matching.same_episode if strict else None),
    )


@pytest.mark.parametrize('strict,expected', [(False, [23]), (True, [])])
def test_wide_refined_interval_only_passes_legacy_matching(source, strict, expected):
    model = SequenceModel([[('R22', 5, 5, 5)], [('R22', 0, 23, 23)]])
    result, metrics = analyzer(model, strict=strict).analyze_with_metrics(source)
    assert [claim.timecode_sec for claim in result.violations] == expected
    assert [call[0] for call in model.calls] == [False, True]
    assert metrics['refinement_confirmed_candidates'] == int(not strict)


def test_refined_peak_can_move_within_screened_interval_without_additional_model_calls(source):
    model = SequenceModel([[('R22', 2, 6, 2)], [('R22', 0, 23, 5)]])
    result = analyzer(model).analyze(source)
    assert [claim.timecode_sec for claim in result.violations] == [5]
    assert len(model.calls) == 2
    assert all(0 < call[2] <= 210 for call in model.calls)
    assert model.calls[1][2] <= model.calls[0][2]


def test_premerge_candidates_are_passed_with_their_original_context(source):
    captured = []

    def matcher(original, refined, *, ignore_rule=False, constituents=None):
        captured.append((original, constituents))
        return episode_matching.same_episode(original, refined, ignore_rule=ignore_rule, constituents=constituents)

    model = SequenceModel([[('R22', 2, 5, 2), ('R22', 4, 8, 4)], [('R22', 2, 8, 7)]])
    result = analyzer(model, matcher=matcher).analyze(source)
    assert result.violations[0].timecode_sec == 7
    merged, constituents = captured[0]
    assert (merged.start, merged.end) == (2, 8)
    assert [(item.start, item.end, item.peak) for item in constituents] == [(2, 5, 2), (4, 8, 4)]
    assert all(item.context_frames == model.calls[0][3] for item in constituents)
    assert len(model.calls) == 2  # Merging still produces one refinement, not two.


def test_real_global_local_merge_preserves_each_screening_resolution(monkeypatch, tmp_path):
    video = Video(21, tuple(Frame(float(i), b'synthetic') for i in range(21)), 21)
    monkeypatch.setattr('app.local_analyzer.decode_video', lambda *args, **kwargs: video)
    context = AnalysisContext('synthetic', tmp_path / 'synthetic.mov', 21, ('R22',))
    captured = []

    def selector(source, start, end, count, *, deadline, audit):
        return [frame for frame in source.frames if start <= frame.timestamp <= start + 2]

    def matcher(original, refined, *, ignore_rule=False, constituents=None):
        captured.append((original, constituents))
        return episode_matching.same_episode(original, refined, ignore_rule=ignore_rule, constituents=constituents)

    class Model:
        def __init__(self):
            self.calls = []

        def infer(self, frames, timeout, *, refine=False, rule_ids=None):
            pts = [frame.timestamp for frame in frames]
            self.calls.append((refine, pts))
            claims = ([event(frames, 'R22', 12, 12, 12)] if refine else
                      [event(frames, 'R22', 10, 10, 10)] if pts in ([0, 10, 20], [9, 10, 11]) else [])
            return WindowResult(assessable=True, events=claims)

    model = Model()
    result = QwenLocalAnalyzer(
        AnalyzerConfig(input_mode='video', frames_per_window=3, window_sec=11, stride_sec=9),
        model=model, local_frame_selector=selector, candidate_matcher=matcher,
    ).analyze(context)
    assert [claim.timecode_sec for claim in result.violations] == [12]
    assert model.calls[:4] == [(False, [0, 10, 20]), (False, [0, 1, 2]),
                              (False, [9, 10, 11]), (False, [18, 19, 20])]
    assert len(model.calls) == 5
    merged, constituents = captured[0]
    assert [[frame.timestamp for frame in item.context_frames] for item in constituents] == [
        [0, 10, 20], [9, 10, 11]]
    assert episode_matching.admissible_trigger_ranges(merged, constituents=constituents) == ((5, 15),)
    assert episode_matching.admissible_trigger_ranges(merged) == ((9.5, 10.5),)


def test_gradual_reclassification_drift_still_checks_initial_screening_episode(monkeypatch, tmp_path):
    pts = [*range(6), 5.5, *range(6, 12)]
    video = Video(12, tuple(Frame(float(t), b'synthetic') for t in pts), len(pts))
    monkeypatch.setattr('app.local_analyzer.decode_video', lambda *args, **kwargs: video)
    context = AnalysisContext('synthetic', tmp_path / 'synthetic.mov', 12, ('R22', 'R20-A'))
    model = SequenceModel([[('R22', 5, 5, 5)], [('R20-A', 5.5, 6, 5.5)], [('R20-A', 6, 6, 6)]])
    checks = []

    def matcher(original, refined, *, ignore_rule=False, constituents=None):
        matched = episode_matching.same_episode(original, refined, ignore_rule=ignore_rule, constituents=constituents)
        checks.append((original.peak, refined.peak, ignore_rule, matched))
        return matched

    result = QwenLocalAnalyzer(
        AnalyzerConfig(input_mode='video', frames_per_window=12, allow_reclassification=True),
        model=model, candidate_matcher=matcher,
    ).analyze(context)
    assert result.violations == []
    assert len(model.calls) == 3
    assert (5, 5.5, True, True) in checks   # The alternative is within initial tolerance.
    assert (5.5, 6, False, True) in checks # It agrees with the new class's interval.
    assert (5, 6, True, False) in checks   # It still cannot escape the original episode.


@pytest.mark.parametrize('alternative_peak,confirmation_peak,expected_calls,expected', [
    (23, None, 2, []),  # Different class and distant peak cannot request confirmation.
    (5, 23, 3, []),    # Second confirmation cannot drift from the original episode.
    (5, 5, 3, [5]),
])
def test_reclassification_cannot_bypass_peak_matching(
        source, alternative_peak, confirmation_peak, expected_calls, expected):
    responses = [[('R22', 5, 5, 5)], [('R20-A', 0, 23, alternative_peak)]]
    if confirmation_peak is not None:
        responses.append([('R20-A', 0, 23, confirmation_peak)])
    model = SequenceModel(responses)
    result = analyzer(model, reclassify=True).analyze(source)
    assert [claim.timecode_sec for claim in result.violations] == expected
    assert len(model.calls) == expected_calls
    if expected_calls == 3:
        assert model.calls[2][1] == ('R20-A',)


@pytest.mark.parametrize('stage', ['alternative', 'confirmation'])
def test_reclassification_does_not_expand_enabled_classes(source, stage):
    # Deliberately bypass the real model validator to verify the analyzer boundary.
    responses = [[('R22', 5, 5, 5)]]
    responses.append([('R20-C' if stage == 'alternative' else 'R20-A', 0, 23, 5)])
    if stage == 'confirmation':
        responses.append([('R20-C', 0, 23, 5)])
    model = SequenceModel(responses)
    result = analyzer(model, reclassify=True).analyze(source)
    assert result.violations == []
    assert len(model.calls) == (2 if stage == 'alternative' else 3)


def test_service_factory_uses_strict_matching_without_other_research_variants(monkeypatch):
    instance = build_analyzer()
    assert instance.candidate_matcher is application_episode_matching.same_episode
    assert instance.local_frame_selector is None
    assert instance.config.allow_reclassification is False
    assert instance.config.complementary_sampling is False


def test_service_factory_rejects_remote_peak_without_extra_model_calls(source, monkeypatch):
    monkeypatch.setenv('APP_ML_INPUT_MODE', 'video')
    monkeypatch.setenv('APP_ML_FRAMES', '24')
    instance = build_analyzer()
    instance.model = SequenceModel([[('R22', 5, 5, 5)], [('R22', 0, 23, 23)]])
    result = instance.analyze(source)
    assert result.violations == []
    assert len(instance.model.calls) == 2


@pytest.mark.parametrize('strict', [False, True])
def test_cli_explicit_flag_wiring_and_code_provenance(tmp_path, monkeypatch, isolated_evaluation_lock, strict):
    source = tmp_path / 'synthetic.mov'
    source.write_bytes(b'synthetic-source-no-decoding')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'records': [{
        'id': 'synthetic', 'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'label': 'no_violation', 'point_sec': None, 'duration_sec': 24, 'split': 'dev',
    }]}))
    output = tmp_path / 'results'
    seen = []

    class Analyzer:
        def __init__(self, config, **kwargs):
            seen.append(kwargs)

        def analyze_with_metrics(self, context):
            return LocalAnalysisResult(verdict='no_violation', violations=[],
                                       checked_rule_ids=list(context.enabled_rule_ids)), {'elapsed_sec': .01}

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(evaluate.MemorySampler, 'run', lambda self: self.stop.wait(2))
    args = ['evaluate', '--manifest', str(manifest), '--output', str(output), '--mode', 'dev']
    if strict:
        args.append('--strict-episode-matching')
    monkeypatch.setattr(sys, 'argv', args)
    evaluate.main()
    assert seen == ([{'candidate_matcher': episode_matching.same_episode}] if strict else [{}])
    provenance = json.loads((output / 'metrics.json').read_text())['provenance']
    assert provenance['strict_episode_matching'] is strict
    assert provenance['episode_matching_policy'] == (episode_matching.MATCHING_POLICY if strict else 'interval-overlap')
    key = 'ml_tools/episode_matching.py'
    if strict:
        assert provenance['component_sha256'][key] == hashlib.sha256(Path(episode_matching.__file__).read_bytes()).hexdigest()
        assert provenance['component_sha256']['app/ml/episode_matching.py'] == hashlib.sha256(
            Path(application_episode_matching.__file__).read_bytes()).hexdigest()
    else:
        assert key not in provenance['component_sha256']
        assert 'app/ml/episode_matching.py' not in provenance['component_sha256']
