"""Fixed catalog: verified text, experimental visual applicability.

Official source: NB RK resolution 231, paragraphs 20 and 22.
https://old.adilet.zan.kz/rus/docs/V1900019680
See docs/legal-basis.md for the source/revision audit; the recording's applicable
revision still requires confirmation. Visual criteria are engineering conditions.
Models cannot choose a legal URL, clause or title.
"""
from dataclasses import dataclass
from app.schemas import RuleReference


@dataclass(frozen=True)
class Rule:
    title: str
    clause: str
    criteria: tuple[str, ...]
    instruction: str


RULES = {
    "R20-A": Rule(
        "Приём или пересчёт внесённых денег без присутствия клиента", "пункт 20, абзац первый",
        ("cash_deposit_observed", "customer_zone_visible", "customer_absence_confirmed", "deposit_counting_observed"),
        "A customer hands over a cash deposit, then visibly leaves the entire service area, "
        "while the cashier is still accepting/counting that deposit. All four facts must be visible. "
        "A cropped/hidden customer or internal cash recount is NOT evidence of this event.",
    ),
    "R20-B": Rule(
        "Ранее принятые деньги остаются на столе при следующем обслуживании", "пункт 20, абзац второй",
        ("previous_deposit_completed", "same_cash_tracked", "next_customer_service_started", "cash_outside_storage"),
        "A previous cash deposit visibly finishes, a new customer starts service, and the same "
        "previous deposit remains outside storage on the work surface. Trace the same cash. "
        "Arrival, standing nearby or waiting is not service: use the first direct cashier interaction "
        "with the new customer (such as accepting their documents or cash) as the triggering frame. "
        "Cash of the current customer, unknown ownership, or money placed in a drawer is NOT this event.",
    ),
    "R22": Rule(
        "Кассир покидает рабочее место до окончания обслуживания", "пункт 22",
        ("cash_deposit_started", "service_unfinished", "whole_workplace_visible", "cashier_leaves_workplace"),
        "Cashier visibly leaves the whole workstation during an ongoing unfinished CUSTOMER CASH DEPOSIT. "
        "Show a customer handing over cash and evidence that acceptance/counting remains unfinished. "
        "An unknown transaction, a withdrawal or internal cash work does not establish this procedure. Turning, bending, standing up, "
        "moving within the workstation, or leaving after service completes is NOT this event.",
    ),
    "R20-C": Rule(
        "Ранее принятые деньги оставлены вне места хранения", "пункт 20, абзац второй",
        ("completed_deposit_observed", "same_cash_tracked", "cash_outside_storage", "cash_not_being_processed"),
        "Money from a visibly completed customer deposit remains on the desk outside storage after service ends. "
        "Track that same cash from the deposit through completion to a subsequent unrelated activity or departure. "
        "The cashier need not leave: the condition is completed service with previous cash left outside storage. "
        "The customer's departure alone does not prove completion if acceptance/counting is still continuing. "
        "Documents alone, an unidentified object, cash still being processed, or a temporary movement to "
        "nearby storage are NOT this event. A new customer need not arrive. If a next customer is served, "
        "report R20-B instead of duplicating R20-C for the same cash.",
    ),
}

SOURCE_URL = "https://adilet.zan.kz/rus/docs/V1900019680"
DOCUMENT_TITLE = "Правила осуществления кассовых операций, постановление НБРК № 231 от 29.11.2019"


def reference(rule_id: str) -> RuleReference:
    return RuleReference(title=DOCUMENT_TITLE, clause=RULES[rule_id].clause, source_url=SOURCE_URL)
