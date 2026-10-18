"""Research-only per-rule screening; never enabled by the service factory.

Separate questions to the same model are not independent confirmation. This
ablation retains the production conditions, transport and response validation.
"""
from __future__ import annotations

import math
import time

from pydantic import ValidationError

from app.ml.model import PROMPT, LoopbackModel, WindowResult
from app.ml.rules import RULES
from app.ml.video import Frame, VideoAnalysisError, check_deadline


_COMBINED_ROLE_HINTS = (
    'For R20-A verify that the person leaving is the depositor; for R22 verify it is the cashier.\n',
    'For R20-C verify completion of the deposit and persistence of the same banknotes, not a mandatory departure.\n',
    'For R20-B verify two different customer service episodes, not a cashier moving to another side of the desk.\n',
)
_ROLE_HINTS = {
    'R20-A': 'Verify that the person leaving is the depositor.',
    'R22': 'Verify that the person leaving is the cashier.',
    'R20-C': 'Verify completion of the deposit and persistence of the same banknotes, not a mandatory departure.',
    'R20-B': 'Verify two different customer service episodes, not a cashier moving to another side of the desk.',
}


def _scoped_prompt(rule_id: str) -> str:
    suffix = '\n'.join(f"{key}: {rule.instruction} Required conditions: {', '.join(rule.criteria)}"
                       for key, rule in RULES.items())
    if not PROMPT.endswith(suffix):
        raise VideoAnalysisError('Production prompt structure changed; review the rulewise research prompt')
    common = PROMPT[:-len(suffix)]
    for hint in _COMBINED_ROLE_HINTS:
        if common.count(hint) != 1:
            raise VideoAnalysisError('Production role guidance changed; review the rulewise research prompt')
        common = common.replace(hint, '')
    rule = RULES[rule_id]
    instruction = rule.instruction
    if rule_id == 'R20-C':
        # Preserve the existing exclusion, but never instruct a C-only request to
        # emit the cross-referenced next-customer class outside its allowed scope.
        instruction = instruction.replace(
            'report R20-B instead of duplicating R20-C for the same cash.',
            'omit R20-C for the same cash; the next-customer procedure is outside this request.')
    prompt = (common + _ROLE_HINTS[rule_id] + '\n' + rule_id + ': ' + instruction
              + ' Required conditions: ' + ', '.join(rule.criteria))
    if any(other in prompt for other in RULES if other != rule_id):
        raise VideoAnalysisError('Rulewise prompt contains an unrelated procedure identifier')
    return prompt


def scoped_prompts(rule_ids: tuple[str, ...]) -> dict[str, str]:
    """Exact prompts for private evaluation provenance, without invoking a model."""
    if not rule_ids or set(rule_ids) - set(RULES) or len(set(rule_ids)) != len(rule_ids):
        raise VideoAnalysisError('Unsupported, repeated or empty rulewise procedure set')
    return {rule_id: _scoped_prompt(rule_id) for rule_id in rule_ids}


class _ScopedRuleModel(LoopbackModel):
    def __init__(self, parent: LoopbackModel, rule_id: str):
        super().__init__(parent.endpoint, max_tokens=parent.max_tokens, input_mode=parent.input_mode)
        self.opener = parent.opener
        self.scoped_prompt = _scoped_prompt(rule_id)

    def complete(self, content: list[dict], prompt: str, schema: dict, timeout: float) -> dict:
        return super().complete(content, self.scoped_prompt, schema, timeout)


class RulewiseModel(LoopbackModel):
    def __init__(self, endpoint: str = 'http://127.0.0.1:8091', *, input_mode: str = 'video',
                 max_tokens: int = 1536):
        super().__init__(endpoint, max_tokens=max_tokens, input_mode=input_mode)
        # Aggregate counters only: no frame bytes, timestamps, descriptions or source IDs.
        self.audit = {
            'batches': 0, 'failed_batches': 0, 'rule_calls': 0,
            'initial_rule_calls': 0, 'refinement_rule_calls': 0,
            'unassessable_responses': 0, 'returned_events': 0,
            'rule_call_counts': {}, 'rule_response_event_counts': {},
        }

    def infer(self, frames: list[Frame], timeout: float, *, refine: bool = False,
              rule_ids: tuple[str, ...] | None = None) -> WindowResult:
        self.audit['batches'] += 1
        try:
            if not math.isfinite(timeout) or timeout <= 0:
                raise VideoAnalysisError('Rulewise analysis time budget exhausted')
            deadline = time.monotonic() + timeout
            active = tuple(RULES) if rule_ids is None else tuple(rule_ids)
            if not active or set(active) - set(RULES) or len(set(active)) != len(active):
                raise VideoAnalysisError('Unsupported, repeated or empty rulewise procedure set')
            events = []
            assessable = True
            for rule_id in active:
                reviewer = _ScopedRuleModel(self, rule_id)
                remaining = check_deadline(deadline)
                self.audit['rule_calls'] += 1
                self.audit['refinement_rule_calls' if refine else 'initial_rule_calls'] += 1
                counts = self.audit['rule_call_counts']
                counts[rule_id] = counts.get(rule_id, 0) + 1
                # Calling inherited infer preserves scope, conditions, frame-index,
                # JSON-schema, loopback and transport validation for every response.
                result = reviewer.infer(frames, remaining, refine=refine, rule_ids=(rule_id,))
                check_deadline(deadline)
                assessable = assessable and result.assessable
                self.audit['unassessable_responses'] += not result.assessable
                counts = self.audit['rule_response_event_counts']
                counts[rule_id] = counts.get(rule_id, 0) + len(result.events)
                events.extend(result.events)
                try:
                    combined = WindowResult(assessable=assessable, events=events)
                except ValidationError:
                    raise VideoAnalysisError('Combined rulewise response exceeded the validated event limit') from None
            self.audit['returned_events'] += len(combined.events)
            return combined
        except VideoAnalysisError:
            self.audit['failed_batches'] += 1
            raise
