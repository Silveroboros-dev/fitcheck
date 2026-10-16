"""Safe rendering helpers for generated fit-card text and provenance.

Accepted source structures and market identifiers retain their original values
in their governed fields.  Generated explanations must not repeat those values
when they contain vocabulary reserved by the response-boundary policy.
"""

from __future__ import annotations

from typing import Protocol

from el.domain.vocabulary import vocabulary_violations

RESTRICTED_SOURCE_WORDING_STATUS = "restricted_source_wording_withheld"


class _CheckDetail(Protocol):
    check_id: str
    name: str
    detail: str


def generated_check_detail(outcome: _CheckDetail) -> str:
    """Return a check explanation that is safe to generate downstream."""
    if not vocabulary_violations(outcome.detail):
        return outcome.detail
    return (
        f"{outcome.check_id} ({outcome.name}): source wording withheld from "
        "generated explanation by the vocabulary guard."
    )


def generated_check_evidence(outcome: _CheckDetail) -> dict[str, str]:
    """Build the safe, attributable detail fields persisted in provenance."""
    rendered = {"detail": generated_check_detail(outcome)}
    if vocabulary_violations(outcome.detail):
        rendered["detail_status"] = RESTRICTED_SOURCE_WORDING_STATUS
    return rendered


def generated_market_label(market_id: str) -> str:
    """Return a safe narrative label while preserving the source identifier."""
    if vocabulary_violations(market_id):
        return "candidate (identifier retained in provenance)"
    return market_id
