"""Synthetic catalog for ML contract tests; never enables deployment rules."""
from datetime import date

import pytest

from app.ml.rules import RULES
from app.rules import RuleCatalog, RuleDefinition
from app.schemas import RuleReference


@pytest.fixture
def synthetic_ml_catalog():
    return RuleCatalog(version='synthetic-test', rules=[RuleDefinition(
        id=key, event_type='Synthetic event', enabled=True, verification='verified',
        verified_on=date(2026, 1, 1),
        reference=RuleReference(title='Synthetic test rule', clause='test clause',
                                source_url='https://example.com/synthetic'),
        observable_conditions=['Synthetic condition'], exclusions=['Synthetic exclusion'],
        verification_note='Test fixture only; no real normative verification.',
    ) for key in RULES])
