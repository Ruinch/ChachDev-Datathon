"""Experimental LOCAL LoRA runner. No downloads, telemetry, publishing or activation.

Default is a preflight. Training requires --train, reviewed exact image sequences,
local unquantized model files and a separately prepared CUDA/Unsloth environment.
This runner trains multi-image input, not llama.cpp's native input_video path.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess


def private_output(path: Path) -> Path:
    path = path.resolve()
    roots = [Path.cwd().resolve() / name for name in ('.local_ml', '.local_data')]
    if not any(root.resolve() == root and path != root and path.is_relative_to(root) for root in roots):
        raise ValueError('Choose a private output under .local_ml/ or .local_data/')
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('Choose a fresh output directory; previous runs are preserved')
    return path


def inspect_model(directory: Path) -> dict:
    """Validate the local file layout, not the numerical contents of the weights."""
    directory = directory.resolve()
    if not directory.is_dir() or not (directory / 'config.json').is_file():
        raise ValueError('A complete local safetensors model directory is required')
    config = json.loads((directory / 'config.json').read_text(encoding='utf-8'))
    if config.get('model_type') != 'qwen3_5':
        raise ValueError('This experimental recipe supports dense Qwen3.5 only')
    if config.get('quantization_config'):
        raise ValueError('This bf16 LoRA recipe does not accept quantized training weights')
    text = config.get('text_config', {})
    shape = (text.get('hidden_size'), text.get('num_hidden_layers'))
    size = {(2048, 24): '2B', (2560, 32): '4B'}.get(shape)
    if not size:
        raise ValueError('Use the supported Qwen3.5 2B or 4B architecture')
    names = ['config.json', 'tokenizer.json', 'tokenizer_config.json', 'preprocessor_config.json']
    template = directory / 'chat_template.jinja'
    tokenizer_config = directory / 'tokenizer_config.json'
    if template.is_file():
        names.append(template.name)
    elif not tokenizer_config.is_file() or not json.loads(tokenizer_config.read_text(encoding='utf-8')).get('chat_template'):
        raise ValueError('Local model is missing its chat template')
    index = directory / 'model.safetensors.index.json'
    if index.is_file():
        weight_names = set(json.loads(index.read_text(encoding='utf-8')).get('weight_map', {}).values())
        names.append(index.name)
    else:
        weight_names = {'model.safetensors'}
    if not weight_names:
        raise ValueError('Local model weights are incomplete')
    for name in weight_names:
        candidate = (directory / name).resolve()
        if not candidate.is_relative_to(directory) or candidate.suffix != '.safetensors' or not candidate.is_file():
            raise ValueError('Local model weights are incomplete or outside the selected directory')
    names.extend(sorted(weight_names))
    if not all((directory / name).is_file() for name in names):
        raise ValueError('Local processor or tokenizer files are incomplete')
    # Additional processor files are preserved in provenance when present.
    names += [name for name in ('processor_config.json', 'video_preprocessor_config.json',
                               'generation_config.json', 'special_tokens_map.json')
              if (directory / name).is_file()]
    return {'size': size, 'files': sorted(set(names)), 'directory': str(directory),
            'config_sha256': hashlib.sha256((directory / 'config.json').read_bytes()).hexdigest()}


def resource_check(size: str, gpu_memory_mib: int, modules: dict[str, bool]) -> dict:
    # Published bf16 LoRA reference minima, not a promise for multi-image windows.
    minimum = {'2B': 5 * 1024, '4B': 10 * 1024}[size]
    blockers = []
    if not all(modules.values()):
        blockers.append('missing_training_packages')
    if gpu_memory_mib < minimum:
        blockers.append('vram_below_reference_minimum')
    return {'can_attempt_training': not blockers, 'blockers': blockers,
            'reference_minimum_mib': minimum, 'gpu_total_mib': gpu_memory_mib,
            'packages': modules, 'notice': 'Multi-image activation memory can exceed this reference. Run one step first.'}


def build_conversations(rows: list[dict], prompt: str) -> list[dict]:
    from PIL import Image

    conversations = []
    for row in rows:
        content = [{'type': 'text', 'text': 'Check this chronological sequence. Report ONLY these procedure IDs: '
                    'R20-A, R20-B, R22, R20-C'}]
        for index, frame in enumerate(row['frames']):
            path = Path(frame['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != frame['sha256']:
                raise ValueError('Reviewed training frame changed before loading')
            content.append({'type': 'text', 'text': f"Frame {index}, original timestamp {frame['timestamp_sec']:.3f} s"})
            with Image.open(path) as image:
                content.append({'type': 'image', 'image': image.convert('RGB').copy()})
        conversations.append({'messages': [
            {'role': 'system', 'content': prompt},
            {'role': 'user', 'content': content},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': json.dumps(row['answer'], ensure_ascii=False)}]},
        ]})
    return conversations


def offline_environment() -> None:
    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_DATASETS_OFFLINE',
                'HF_HUB_DISABLE_TELEMETRY', 'DO_NOT_TRACK', 'UNSLOTH_DISABLE_STATISTICS'):
        os.environ[key] = '1'
    os.environ['WANDB_MODE'] = 'disabled'


def gpu_memory() -> int:
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return int(result.stdout.splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


class CompleteSequenceCollator:
    """Reject over-budget sequences instead of teaching from truncated JSON.

    Unsloth normally inherits the model's length limit. Disabling both explicit
    attributes is necessary before measuring the real processed token count.
    Reject an incompatible collator API rather than assuming it is unbounded.
    """
    def __init__(self, collator, max_length: int):
        if not all(hasattr(collator, name) for name in ('max_seq_length', 'truncation')):
            raise ValueError('Unsupported vision collator API; verify the nontruncating configuration')
        collator.max_seq_length = None
        collator.truncation = False
        self.collator = collator
        self.max_length = max_length

    def __call__(self, rows):
        batch = self.collator(rows)
        if batch['input_ids'].shape[-1] > self.max_length:
            raise ValueError('A complete reviewed sequence exceeds the token budget; prepare fewer/smaller frames')
        if not (batch['labels'] != -100).any().item():
            raise ValueError('Assistant response mask contains no training tokens; inspect the local chat template')
        return batch


def train(bundle: dict, model_info: dict, output: Path, *, max_steps: int = 1, max_length: int = 4096) -> None:
    offline_environment()
    # Unsloth must patch before importing Transformers/TRL.
    from unsloth import FastVisionModel
    from unsloth.trainer import UnslothVisionDataCollator
    import torch
    from trl import SFTConfig, SFTTrainer

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise ValueError('This bf16 recipe needs a compatible local CUDA GPU')
    if torch.cuda.mem_get_info()[0] < {'2B': 5 * 2**30, '4B': 10 * 2**30}[model_info['size']]:
        raise ValueError('Not enough free CUDA memory; stop the local inference runtime before training')
    output = private_output(output)
    output.mkdir(parents=True, exist_ok=True)
    versions = {name: importlib.metadata.version(name) for name in ('torch', 'transformers', 'trl', 'unsloth', 'peft')}
    model_root = Path(model_info['directory'])
    hashes = {}
    for name in model_info['files']:
        with (model_root / name).open('rb') as source:
            hashes[name] = hashlib.file_digest(source, 'sha256').hexdigest()
    provenance = {'status': 'started', 'experimental': True, 'training_input_mode': 'images',
        'model_size': model_info['size'], 'base_files_sha256': hashes,
        'prompt_sha256': bundle['prompt_sha256'], 'data_summary': bundle['summary'],
        'versions': versions, 'steps': max_steps, 'max_length': max_length,
        'quality_accepted': False, 'deployment_activated': False}
    # Hash labels and frames without copying them into shareable reports.
    provenance['reviewed_data_sha256'] = hashlib.sha256(json.dumps(
        {part: bundle[part] for part in ('train', 'validation')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    report = output / 'training-run.json'
    report.write_text(json.dumps(provenance, indent=2), encoding='utf-8')
    try:
        model, processor = FastVisionModel.from_pretrained(
            model_name=str(model_root), load_in_4bit=False, load_in_16bit=True,
            dtype=torch.bfloat16, use_gradient_checkpointing='unsloth',
            local_files_only=True, trust_remote_code=False, max_seq_length=max_length)
        model = FastVisionModel.get_peft_model(model, finetune_vision_layers=False,
            finetune_language_layers=True, finetune_attention_modules=True, finetune_mlp_modules=False,
            r=8, lora_alpha=8, lora_dropout=0, bias='none', random_state=26)
        FastVisionModel.for_training(model)
        dataset = build_conversations(bundle['train'], bundle['prompt'])
        validation = build_conversations(bundle['validation'], bundle['prompt'])
        collator = CompleteSequenceCollator(UnslothVisionDataCollator(model, processor, resize='max',
            train_on_responses_only=True, instruction_part='<|im_start|>user\n',
            response_part='<|im_start|>assistant\n'), max_length)
        # Verify the real processor's tokenization before training; never silently
        # truncate frames or the JSON answer to meet the memory limit.
        for row in dataset + validation:
            batch = collator([row])
            del batch
        trainer = SFTTrainer(model=model, processing_class=processor, data_collator=collator,
            train_dataset=dataset, eval_dataset=validation,
            args=SFTConfig(output_dir=str(output / 'checkpoints'), per_device_train_batch_size=1,
                per_device_eval_batch_size=1, gradient_accumulation_steps=4,
                max_steps=max_steps, learning_rate=2e-5, warmup_steps=0 if max_steps == 1 else max(1, max_steps // 10),
                bf16=True, fp16=False, optim='adamw_torch', logging_steps=1,
                save_strategy='no', eval_strategy='no', report_to='none', push_to_hub=False,
                remove_unused_columns=False, dataset_text_field='',
                dataset_kwargs={'skip_prepare_dataset': True}, dataset_num_proc=1,
                dataloader_num_workers=0, max_length=None, seed=26))
        trainer.train()
        evaluation = trainer.evaluate()
        model.save_pretrained(str(output / 'adapter'))
        processor.save_pretrained(str(output / 'adapter'))
        provenance.update(status='completed', validation_loss=evaluation.get('eval_loss'),
                          gpu_peak_allocated_mib=round(torch.cuda.max_memory_allocated() / 2**20))
    except Exception:
        # Do not persist exception payloads: processor errors can include a sample.
        provenance['status'] = 'failed'
        raise
    finally:
        report.write_text(json.dumps(provenance, indent=2), encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('.local_ml/training/run-01'))
    parser.add_argument('--train', action='store_true', help='Actually update LoRA weights; default only checks readiness')
    parser.add_argument('--max-steps', type=int, default=1, help='Start with one local smoke step, then choose a bounded experiment')
    parser.add_argument('--max-length', type=int, default=4096)
    args = parser.parse_args()
    offline_environment()
    stage = 'configuration'
    try:
        if not 1 <= args.max_steps <= 200 or not 1024 <= args.max_length <= 8192:
            raise ValueError('Invalid bounded training configuration')
        from ml_tools.training_data import compile_review
        stage = 'reviewed_training_data'
        bundle = compile_review(args.review)
        stage = 'local_model_files'
        info = inspect_model(args.model_dir)
        stage = 'resources'
        output = private_output(args.output)
        resources = resource_check(info['size'], gpu_memory(),
            {name: importlib.util.find_spec(name) is not None for name in ('torch', 'unsloth', 'trl', 'transformers')})
        print(json.dumps({'data': bundle['summary'], 'model_size': info['size'], **resources}))
        if not resources['can_attempt_training']:
            return 2
        if args.train:
            stage = 'cuda_training'
            train(bundle, info, output, max_steps=args.max_steps, max_length=args.max_length)
            print(json.dumps({'status': 'completed', 'quality_accepted': False, 'deployment_activated': False}))
        return 0
    except Exception as error:
        # No tracebacks or raw values from annotation/model validation in shared logs.
        print(json.dumps({'status': 'blocked_or_failed', 'stage': stage, 'error_type': type(error).__name__,
                          'notice': 'Check local reviewed data, model files, CUDA environment and training-run.json. No model was activated.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
