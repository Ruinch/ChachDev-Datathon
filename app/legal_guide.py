"""Read the explanatory legal guide without changing executable analysis rules."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, field_validator, model_validator


DEFAULT_LEGAL_GUIDE = Path(__file__).resolve().parent / "data" / "legal-guide.json"
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_OFFICIAL_HOSTS = ("adilet.zan.kz", "old.adilet.zan.kz", "zan.gov.kz", "nationalbank.kz", "gov.kz")


class _GuideModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LegalSource(_GuideModel):
    id: Text
    title: Text
    url: Text
    locator: Text
    edition_note: Text
    checked_on: date
    verification_status: Literal["checked", "historical", "unavailable"]

    @field_validator("url")
    @classmethod
    def official_https_source(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        official = any(host == allowed or host.endswith("." + allowed) for allowed in _OFFICIAL_HOSTS)
        if (parsed.scheme != "https" or not official or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            raise ValueError("A legal source must use an official Kazakhstan HTTPS address without credentials")
        return value


class LegalPrinciple(_GuideModel):
    title: Text
    text: Text


class LegalProcedure(_GuideModel):
    id: Text
    title: Text
    related_rule_ids: list[Text]
    source_ids: list[Text]
    basis: Literal["national_rule", "internal_policy", "interpretation"]
    requirement: Text
    correct: list[Text]
    potential_deviation: list[Text]
    required_context: list[Text]
    cannot_conclude: list[Text]
    notes: list[Text]


class LegalGuide(_GuideModel):
    version: Text
    reviewed_on: date
    jurisdiction: Literal["KZ"]
    purpose: Text
    result_binding: Literal["reference_only"]
    sources: list[LegalSource]
    principles: list[LegalPrinciple]
    procedures: list[LegalProcedure]

    @model_validator(mode="after")
    def valid_references(self) -> "LegalGuide":
        source_ids = [source.id for source in self.sources]
        procedure_ids = [procedure.id for procedure in self.procedures]
        if len(set(source_ids)) != len(source_ids) or len(set(procedure_ids)) != len(procedure_ids):
            raise ValueError("Legal source and procedure IDs must each be unique")
        known_sources = set(source_ids)
        for procedure in self.procedures:
            if not set(procedure.source_ids) <= known_sources:
                raise ValueError("A legal procedure references an unknown source")
        return self


class LegalGuideUnavailableError(RuntimeError):
    """The local explanatory document cannot be safely presented."""


def load_legal_guide(path: Path | str | None = None) -> LegalGuide:
    guide_path = Path(path) if path is not None else DEFAULT_LEGAL_GUIDE
    try:
        return LegalGuide.model_validate_json(guide_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise LegalGuideUnavailableError("The local legal guide is missing or invalid") from exc
