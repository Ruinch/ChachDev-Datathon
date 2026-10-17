from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.analyzer import AnalyzerConfigurationError, RuleValidationError
from app.schemas import RuleReference


DEFAULT_RULE_CATALOG = Path(__file__).resolve().parent / "data" / "rules.json"


class RuleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,63}$")
    event_type: str = Field(min_length=1, max_length=200)
    enabled: bool = False
    verification: str = Field(pattern=r"^(candidate|verified)$")
    verified_on: date | None = None
    reference: RuleReference
    observable_conditions: list[str] = Field(min_length=1)
    exclusions: list[str] = Field(min_length=1)
    verification_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def enabled_requires_verification(self) -> "RuleDefinition":
        if self.enabled and (self.verification != "verified" or self.verified_on is None):
            raise ValueError("An enabled rule requires verified source and visual conditions with a verification date")
        if any(not item.strip() for item in self.observable_conditions + self.exclusions):
            raise ValueError("Rule conditions and exclusions must contain text")
        return self


class RuleCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: str = Field(min_length=1)
    rules: list[RuleDefinition]

    @model_validator(mode="after")
    def unique_rule_ids(self) -> "RuleCatalog":
        identifiers = [rule.id for rule in self.rules]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Rule IDs must be unique")
        return self

    @property
    def enabled_rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.id for rule in self.rules if rule.enabled)

    def require_enabled(self, rule_id: str) -> RuleDefinition:
        for rule in self.rules:
            if rule.id == rule_id:
                if not rule.enabled:
                    raise RuleValidationError("The analyzer referenced a disabled rule")
                return rule
        raise RuleValidationError("The analyzer referenced an unknown rule")


def load_rule_catalog(path: Path | str | None = None) -> RuleCatalog:
    catalog_path = Path(path) if path is not None else DEFAULT_RULE_CATALOG
    try:
        return RuleCatalog.model_validate_json(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise AnalyzerConfigurationError("The local rule catalog is missing or invalid") from exc
