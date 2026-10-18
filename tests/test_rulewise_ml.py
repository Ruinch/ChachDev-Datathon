from __future__ import annotations

import json

import pytest

from app.ml.rules import RULES
from app.ml.video import Frame, VideoAnalysisError
from ml_tools.rulewise import RulewiseModel


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(self.payload),
        }}]}).encode()


class RecordingOpener:
    def __init__(self, payloads=None):
        self.calls = []
        self.payloads = payloads or []

    def open(self, request, *, timeout):
        self.calls.append((json.loads(request.data), timeout))
        index = len(self.calls) - 1
        payload = self.payloads[index] if index < len(self.payloads) else {'assessable': True, 'events': []}
        return Response(payload)


def frames():
    return [Frame(0.25, b'synthetic-jpeg-a'), Frame(1.75, b'synthetic-jpeg-b')]


def test_screening_uses_one_scoped_request_per_allowed_rule_without_expanding_scope():
    model = RulewiseModel(input_mode='images')
    opener = model.opener = RecordingOpener()
    result = model.infer(frames(), 10, rule_ids=('R20-A', 'R22'))

    assert result.assessable and result.events == []
    assert len(opener.calls) == 2
    for (payload, _), rule_id in zip(opener.calls, ('R20-A', 'R22')):
        prompt = payload['messages'][0]['content']
        assert RULES[rule_id].instruction in prompt
        assert all(criterion in prompt for criterion in RULES[rule_id].criteria)
        assert all(other_id not in prompt for other_id in RULES if other_id != rule_id)
        assert payload['response_format']['json_schema']['schema']['$defs']['Event']['properties']['rule_id']['enum'] == [rule_id]
    assert model.audit['initial_rule_calls'] == 2
    assert model.audit['rule_call_counts'] == {'R20-A': 1, 'R22': 1}


def event(rule_id, **changes):
    value = {'rule_id': rule_id, 'start_frame': 0, 'peak_frame': 1, 'end_frame': 1,
             'conditions': list(RULES[rule_id].criteria), 'observation': 'Synthetic visible action.'}
    value.update(changes)
    return value


def test_cash_storage_scope_retains_next_customer_exclusion_without_requesting_another_rule():
    model = RulewiseModel(input_mode='images')
    opener = model.opener = RecordingOpener()
    model.infer(frames(), 10, rule_ids=('R20-C',))
    prompt = opener.calls[0][0]['messages'][0]['content']
    assert 'If a next customer is served, omit R20-C for the same cash' in prompt
    assert 'R20-B' not in prompt
    assert all(criterion in prompt for criterion in RULES['R20-C'].criteria)


def test_refinement_calls_only_requested_rule_and_preserves_source_mapping():
    model = RulewiseModel(input_mode='images')
    opener = model.opener = RecordingOpener()
    model.infer(frames(), 10, refine=True, rule_ids=('R22',))
    assert len(opener.calls) == 1
    content = opener.calls[0][0]['messages'][1]['content']
    assert content[0]['text'].startswith('Independently recheck this sequence;')
    assert content[1]['text'] == 'Frame 0, original timestamp 0.250 s'
    assert content[3]['text'] == 'Frame 1, original timestamp 1.750 s'
    assert model.audit['refinement_rule_calls'] == 1
    assert model.audit['initial_rule_calls'] == 0


def test_each_rule_receives_only_remaining_time_from_shared_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('ml_tools.rulewise.time.monotonic', lambda: clock[0])

    class SlowOpener(RecordingOpener):
        def open(self, request, *, timeout):
            response = super().open(request, timeout=timeout)
            clock[0] += 2
            return response

    model = RulewiseModel(input_mode='images')
    opener = model.opener = SlowOpener()
    model.infer(frames(), 10)
    assert [timeout for _, timeout in opener.calls] == [10, 8, 6, 4]


def test_exhausted_rulewise_deadline_prevents_following_rule_call(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('ml_tools.rulewise.time.monotonic', lambda: clock[0])

    class ExpiringOpener(RecordingOpener):
        def open(self, request, *, timeout):
            response = super().open(request, timeout=timeout)
            clock[0] += 11
            return response

    model = RulewiseModel(input_mode='images')
    opener = model.opener = ExpiringOpener()
    with pytest.raises(VideoAnalysisError, match='time budget exhausted'):
        model.infer(frames(), 10, rule_ids=('R20-A', 'R22'))
    assert len(opener.calls) == 1
    assert model.audit['failed_batches'] == 1


def test_rule_results_merge_without_erasing_unassessable_context_or_exposing_content():
    model = RulewiseModel(input_mode='images')
    model.opener = RecordingOpener([
        {'assessable': False, 'events': []},
        {'assessable': True, 'events': [event('R22')]},
    ])
    result = model.infer(frames(), 10, rule_ids=('R20-A', 'R22'))
    assert not result.assessable
    assert [item.rule_id for item in result.events] == ['R22']
    assert model.audit['unassessable_responses'] == 1
    assert model.audit['returned_events'] == 1
    assert model.audit['rule_response_event_counts'] == {'R20-A': 0, 'R22': 1}
    audit_json = json.dumps(model.audit)
    assert 'Synthetic visible action' not in audit_json
    assert '0.25' not in audit_json
    assert 'synthetic-jpeg' not in audit_json


@pytest.mark.parametrize('invalid_event', [
    event('R20-A'),
    event('R22', peak_frame=9, end_frame=9),
    event('R22', conditions=['wrong', 'wrong', 'wrong', 'wrong']),
    event('R22', start_frame=True),
])
def test_inherited_event_validation_rejects_bad_scope_indices_and_conditions(invalid_event):
    model = RulewiseModel(input_mode='images')
    model.opener = RecordingOpener([{'assessable': True, 'events': [invalid_event]}])
    with pytest.raises(VideoAnalysisError):
        model.infer(frames(), 10, rule_ids=('R22',))
    assert model.audit['failed_batches'] == 1
    assert model.audit['returned_events'] == 0


def test_invalid_json_remains_a_safe_analysis_error():
    class InvalidJSONResponse(Response):
        def read(self, size):
            return b'{"choices":[{"finish_reason":"stop","message":{"content":"not json"}}]}'

    class InvalidJSONOpener(RecordingOpener):
        def open(self, request, *, timeout):
            super().open(request, timeout=timeout)
            return InvalidJSONResponse(None)

    model = RulewiseModel(input_mode='images')
    model.opener = InvalidJSONOpener()
    with pytest.raises(VideoAnalysisError, match='invalid JSON'):
        model.infer(frames(), 10, rule_ids=('R22',))


def test_combined_event_limit_is_rejected_without_silent_truncation():
    model = RulewiseModel(input_mode='images')
    opener = model.opener = RecordingOpener([
        {'assessable': True, 'events': [event('R20-A')] * 5},
        {'assessable': True, 'events': [event('R22')] * 5},
    ])
    with pytest.raises(VideoAnalysisError, match='event limit'):
        model.infer(frames(), 10, rule_ids=('R20-A', 'R22', 'R20-C'))
    assert len(opener.calls) == 2
    assert model.audit['returned_events'] == 0
    assert model.audit['failed_batches'] == 1


@pytest.mark.parametrize('rule_ids', [(), ('unknown',), ('R22', 'R22')])
def test_invalid_request_scope_fails_before_network(rule_ids):
    model = RulewiseModel(input_mode='images')
    opener = model.opener = RecordingOpener()
    with pytest.raises(VideoAnalysisError, match='procedure set'):
        model.infer(frames(), 10, rule_ids=rule_ids)
    assert opener.calls == []


def test_changed_production_prompt_requires_review_instead_of_unscoped_fallback(monkeypatch):
    monkeypatch.setattr('ml_tools.rulewise.PROMPT', 'Changed production prompt')
    model = RulewiseModel(input_mode='images')
    opener = model.opener = RecordingOpener()
    with pytest.raises(VideoAnalysisError, match='prompt structure changed'):
        model.infer(frames(), 10, rule_ids=('R22',))
    assert opener.calls == []
