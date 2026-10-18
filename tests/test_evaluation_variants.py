from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from app.analyzer import LocalAnalysisResult
from app.ml.model import PROMPT
from app.ml.video import Frame
from ml_tools import evaluate
from ml_tools import rulewise as rulewise_module
from ml_tools.rulewise import RulewiseModel


@pytest.fixture
def evaluation_args(tmp_path, monkeypatch, isolated_evaluation_lock):
    source = tmp_path / 'synthetic.mov'
    source.write_bytes(b'CLI-only synthetic source')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'records': [{
        'id': 'synthetic-example', 'path': str(source),
        'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'label': 'no_violation', 'point_sec': None, 'duration_sec': 3, 'split': 'dev',
    }]}))
    output = tmp_path / 'evaluation'
    monkeypatch.setattr(evaluate.MemorySampler, 'run', lambda self: self.stop.wait(2))
    args = ['evaluate', '--manifest', str(manifest), '--output', str(output), '--mode', 'dev']
    return args, output


@pytest.mark.parametrize('complementary,rulewise', [(False, False), (True, False), (False, True), (True, True)])
def test_research_flags_wire_explicit_variants_without_changing_defaults(
        evaluation_args, monkeypatch, complementary, rulewise):
    args, output = evaluation_args
    seen = []

    class Analyzer:
        def __init__(self, config, *, model=None):
            seen.append((config, model))

        def analyze_with_metrics(self, context):
            return LocalAnalysisResult(
                verdict='no_violation', violations=[], checked_rule_ids=list(context.enabled_rule_ids),
            ), {'elapsed_sec': 0.1}

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    if complementary:
        args.append('--complementary-sampling')
    if rulewise:
        args.append('--rulewise-model')
    monkeypatch.setattr(sys, 'argv', args)
    evaluate.main()

    assert seen[0][0].complementary_sampling is complementary
    assert seen[0][0].sample_fps == 2
    assert isinstance(seen[0][1], RulewiseModel) is rulewise
    report = json.loads((output / 'metrics.json').read_text())
    assert report['configuration']['complementary_sampling'] is complementary
    assert report['configuration']['sample_fps'] == 2
    assert report['provenance']['rulewise_model'] is rulewise
    assert report['provenance']['motion_sampling'] is False
    assert report['provenance']['sampling_policy'] == 'uniform'
    if not rulewise:
        assert report['provenance']['prompt_sha256'] == hashlib.sha256(PROMPT.encode('utf-8')).hexdigest()
        assert 'rulewise_prompts' not in report['provenance']


@pytest.mark.parametrize('flags', [
    ['--rulewise-model', '--observation-model', '--input-mode', 'video'],
    ['--observation-model'],
    ['--frames', '1', '--complementary-sampling'],
    ['--frames', '32', '--input-mode', 'images', '--rulewise-model'],
    ['--budget-sec', '0'],
    ['--limit', '0'],
    ['--sample-fps', '0'],
    ['--sample-fps', '8'],
])
def test_invalid_variant_or_configuration_fails_before_creating_output(evaluation_args, monkeypatch, flags):
    args, output = evaluation_args

    class NeverAnalyzer:
        def __init__(self, *args, **kwargs):
            pytest.fail('Invalid CLI configuration must not create an analyzer')

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', NeverAnalyzer)
    monkeypatch.setattr(sys, 'argv', args + flags)
    with pytest.raises(SystemExit) as error:
        evaluate.main()
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize('fps', [1, 4])
def test_explicit_research_sample_rate_reaches_analyzer_and_saved_configuration(
        evaluation_args, monkeypatch, fps):
    args, output = evaluation_args
    seen = []

    class Analyzer:
        def __init__(self, config):
            seen.append(config)

        def analyze_with_metrics(self, context):
            return LocalAnalysisResult(verdict='no_violation', violations=[]), {'elapsed_sec': 0.1}

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(sys, 'argv', args + ['--sample-fps', str(fps)])
    evaluate.main()
    report = json.loads((output / 'metrics.json').read_text())
    assert seen[0].sample_fps == fps
    assert seen[0].frames_per_window == 12
    assert report['configuration']['sample_fps'] == fps
    assert report['configuration']['frames_per_window'] == 12
    assert report['provenance']['sampling_policy'] == 'uniform'


def test_rulewise_provenance_records_exact_transmitted_prompts_and_code_hash(evaluation_args, monkeypatch):
    args, output = evaluation_args
    transmitted_prompts = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"assessable\\":true,\\"events\\":[]}"}}]}'

    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url == 'http://127.0.0.1:8091/v1/chat/completions'
            payload = json.loads(request.data)
            active = payload['response_format']['json_schema']['schema']['$defs']['Event']['properties']['rule_id']['enum']
            assert len(active) == 1
            transmitted_prompts[active[0]] = payload['messages'][0]['content']
            return Response()

    class Analyzer:
        def __init__(self, config, *, model):
            self.model = model
            self.model.opener = Opener()

        def analyze_with_metrics(self, context):
            self.model.infer([Frame(0.25, b'fixture-a'), Frame(2.75, b'fixture-b')],
                             10, rule_ids=context.enabled_rule_ids)
            return LocalAnalysisResult(
                verdict='no_violation', violations=[], checked_rule_ids=list(context.enabled_rule_ids),
            ), {'elapsed_sec': 0.1}

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(sys, 'argv', args + ['--rulewise-model'])
    evaluate.main()
    report = json.loads((output / 'metrics.json').read_text())
    provenance = report['provenance']
    assert set(transmitted_prompts) == set(report['rule_ids'])
    assert provenance['rulewise_prompts'] == transmitted_prompts
    assert provenance['rulewise_prompt_sha256'] == {
        rule_id: hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        for rule_id, prompt in transmitted_prompts.items()
    }
    canonical = json.dumps(transmitted_prompts, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert provenance['prompt_sha256'] == hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    assert provenance['prompt_format'] == 'canonical_rulewise_prompt_map'
    assert provenance['component_sha256']['ml_tools/rulewise.py'] == hashlib.sha256(
        Path(rulewise_module.__file__).read_bytes()).hexdigest()


def test_existing_observation_variant_still_uses_video_model(evaluation_args, monkeypatch):
    from ml_tools.observation import ObservationModel, OBSERVATION_PROMPT

    args, output = evaluation_args
    seen = []

    class Analyzer:
        def __init__(self, config, *, model):
            seen.append(model)

        def analyze_with_metrics(self, context):
            return LocalAnalysisResult(
                verdict='no_violation', violations=[], checked_rule_ids=list(context.enabled_rule_ids),
            ), {'elapsed_sec': 0.1}

    monkeypatch.setattr(evaluate, 'QwenLocalAnalyzer', Analyzer)
    monkeypatch.setattr(sys, 'argv', args + ['--observation-model', '--input-mode', 'video'])
    evaluate.main()
    assert isinstance(seen[0], ObservationModel)
    report = json.loads((output / 'metrics.json').read_text())
    assert report['provenance']['observation_model']
    assert not report['provenance']['rulewise_model']
    assert report['provenance']['prompt_sha256'] == hashlib.sha256(OBSERVATION_PROMPT.encode('utf-8')).hexdigest()
