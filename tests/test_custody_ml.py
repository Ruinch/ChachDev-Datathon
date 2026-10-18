"""Synthetic cash-custody counterexamples, with no dataset fixtures."""
import io
import pytest

pytest.importorskip('av')
Image = pytest.importorskip('PIL.Image')

from app.ml.model import Event, WindowResult
from app.ml.rules import RULES
from app.ml.video import Frame
from ml_tools.custody import CustodyModel


def synthetic_frames():
    stream = io.BytesIO()
    Image.new('RGB', (64, 64), 'blue').save(stream, format='JPEG')
    return [Frame(i, stream.getvalue()) for i in range(5)]


@pytest.mark.parametrize('rule', ['R20-B', 'R20-C'])
def test_stored_cash_breaks_previous_deposit_chain(rule):
    class Base:
        def infer(self, frames, timeout, **kwargs):
            return WindowResult(assessable=True, events=[Event(
                rule_id=rule, start_frame=1, peak_frame=3, end_frame=4,
                conditions=list(RULES[rule].criteria), observation='Synthetic previous cash claim.')])
        def complete(self, content, prompt, schema, timeout):
            return {'cash_path': 'stored_before_trigger', 'deposit_frame': 0,
                    'storage_frame': 2, 'same_cash_at_trigger_frame': None}
    model = CustodyModel(base=Base())
    result = model.infer(synthetic_frames(), 20, refine=True)
    assert result.assessable and result.events == []


@pytest.mark.parametrize('path,storage,tracked,expected,assessable', [
    ('same_cash_outside_at_trigger', None, 3, 1, True),
    ('unknown', None, None, 0, False),
    ('stored_before_trigger', 4, None, 0, False),
    ('same_cash_outside_at_trigger', 2, 3, 0, False),
    ('same_cash_outside_at_trigger', None, 4, 0, False),
])
def test_custody_requires_visible_consistent_timeline(path, storage, tracked, expected, assessable):
    class Base:
        def infer(self, frames, timeout, **kwargs):
            return WindowResult(assessable=True, events=[Event(
                rule_id='R20-B', start_frame=1, peak_frame=3, end_frame=4,
                conditions=list(RULES['R20-B'].criteria), observation='Synthetic previous cash claim.')])
        def complete(self, content, prompt, schema, timeout):
            return {'cash_path': path, 'deposit_frame': 0,
                    'storage_frame': storage, 'same_cash_at_trigger_frame': tracked}
    result = CustodyModel(base=Base()).infer(synthetic_frames(), 20, refine=True)
    assert len(result.events) == expected
    assert result.assessable is assessable
