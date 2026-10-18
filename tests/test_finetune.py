"""Training preparation tests use generated pixels, never bank recordings."""
import hashlib
import json

import pytest
from PIL import Image

from ml_tools.finetune import build_conversations, inspect_model, resource_check, private_output, CompleteSequenceCollator


def test_conversations_preserve_exact_frame_order_and_targets_without_source_labels(tmp_path):
    frames = []
    for index, timestamp in enumerate([1.25, 7.5]):
        path = tmp_path / f'private-source-name-{index}.jpg'
        Image.new('RGB', (32, 32), (index * 100, 20, 50)).save(path)
        frames.append(dict(path=str(path), timestamp_sec=timestamp,
                           sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    answer = {'assessable': False, 'events': []}
    result = build_conversations([dict(frames=frames, answer=answer)], 'Frozen procedure prompt')
    messages = result[0]['messages']
    assert messages[0] == {'role': 'system', 'content': 'Frozen procedure prompt'}
    content = messages[1]['content']
    texts = ' '.join(p['text'] for p in content if p['type'] == 'text')
    assert 'Frame 0, original timestamp 1.250 s' in texts
    assert 'Frame 1, original timestamp 7.500 s' in texts
    assert 'private-source-name' not in texts
    assert [p['image'].getpixel((0, 0))[0] for p in content if p['type'] == 'image'] == [0, 100]
    assert json.loads(messages[2]['content'][0]['text']) == answer


def test_modified_frame_cannot_enter_training(tmp_path):
    path = tmp_path / 'frame.jpg'
    Image.new('RGB', (32, 32), 'red').save(path)
    frame = dict(path=str(path), timestamp_sec=0, sha256='0' * 64)
    with pytest.raises(ValueError, match='changed'):
        build_conversations([dict(frames=[frame], answer={})], 'prompt')


def test_resources_distinguish_small_experiment_from_4b_training():
    modules = {name: True for name in ('torch', 'unsloth', 'trl', 'transformers')}
    assert resource_check('2B', 6144, modules)['can_attempt_training'] is True
    result = resource_check('4B', 6144, modules)
    assert result['can_attempt_training'] is False
    assert 'vram_below_reference_minimum' in result['blockers']
    modules['torch'] = False
    assert 'missing_training_packages' in resource_check('2B', 6144, modules)['blockers']


def test_local_model_requires_complete_unquantized_safetensors(tmp_path):
    config = {'model_type': 'qwen3_5', 'text_config': {'hidden_size': 2048, 'num_hidden_layers': 24}}
    (tmp_path / 'config.json').write_text(json.dumps(config))
    for name in ('tokenizer.json', 'tokenizer_config.json', 'preprocessor_config.json'):
        (tmp_path / name).write_text('{}')
    (tmp_path / 'chat_template.jinja').write_text('local template')
    (tmp_path / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': {'a': 'missing.safetensors'}}))
    with pytest.raises(ValueError, match='weights'):
        inspect_model(tmp_path)
    (tmp_path / 'missing.safetensors').write_bytes(b'fixture only; not real weights')
    assert inspect_model(tmp_path)['size'] == '2B'
    config['quantization_config'] = {'load_in_4bit': True}
    (tmp_path / 'config.json').write_text(json.dumps(config))
    with pytest.raises(ValueError, match='quantized'):
        inspect_model(tmp_path)


def test_training_artifacts_stay_private_and_existing_runs_are_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert private_output(tmp_path / '.local_ml' / 'new-run').name == 'new-run'
    with pytest.raises(ValueError, match='private'):
        private_output(tmp_path / 'public-run')
    run = tmp_path / '.local_ml' / 'used-run'
    run.mkdir(parents=True)
    (run / 'result.json').write_text('{}')
    with pytest.raises(ValueError, match='fresh'):
        private_output(run)


def test_collation_rejects_overlong_target_before_silent_truncation():
    class PartialMask:
        def __ne__(self, other): return self
        def any(self): return self
        def item(self): return True
    class Tensor:
        def __init__(self, length): self.shape = (1, length)
    class ProcessorCollator:
        max_seq_length = 1024
        truncation = True
        def __call__(self, rows):
            # A retained partial assistant target must not turn truncation green.
            length = self.max_seq_length if self.truncation else 6000
            return {'input_ids': Tensor(length), 'labels': PartialMask()}
    bounded = CompleteSequenceCollator(ProcessorCollator(), 4096)
    with pytest.raises(ValueError, match='token budget'):
        bounded([{'messages': 'Synthetic complete sequence'}])
