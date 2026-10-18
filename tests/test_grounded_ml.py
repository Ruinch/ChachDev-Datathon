"""Synthetic responses at the existing model inference interface; no bank data."""
import json
import pytest
from app.ml.rules import RULES
from app.ml.video import Frame, VideoAnalysisError
from ml_tools.grounded import GroundedModel


def call(payload):
    model = GroundedModel(input_mode='images')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(payload)}}]}).encode()
    class Opener:
        def open(self, request, **kwargs):
            assert request.full_url == 'http://127.0.0.1:8091/v1/chat/completions'
            return Response()
    model.opener=Opener()
    return model.infer([Frame(float(i),b'synthetic') for i in range(5)],5,rule_ids=('R22',))


def payload(**changes):
    event={'rule_id':'R22','start_frame':2,'peak_frame':2,'end_frame':4,
           'facts':[{'condition':key,'state':'observed','frames':[i]} for i,key in enumerate(RULES['R22'].criteria)],
           'alternative':'none_visible','observation':'Synthetic cashier departure during visible deposit.'}
    event.update(changes)
    return {'assessable':True,'events':[event]}


def test_unseen_deposit_cannot_be_promoted_to_r22():
    p=payload()
    p['events'][0]['facts'][0].update(state='not_visible',frames=[])
    result=call(p)
    assert result.events == []
    assert result.assessable is False


def test_normal_alternative_rejects_claim():
    result=call(payload(alternative='ordinary_workstation_movement'))
    assert result.events == []


def test_supported_event_keeps_indices_and_condition_contract():
    result=call(payload())
    assert result.assessable
    assert result.events[0].rule_id=='R22'
    assert result.events[0].peak_frame==2
    assert result.events[0].conditions==list(RULES['R22'].criteria)


def test_fabricated_frame_index_fails_instead_of_becoming_evidence():
    p=payload(); p['events'][0]['facts'][0]['frames']=[99]
    with pytest.raises(VideoAnalysisError): call(p)


def test_cash_handover_after_departure_cannot_support_r22():
    p=payload(); p['events'][0]['facts'][0]['frames']=[4]
    result=call(p)
    assert result.events==[] and not result.assessable
