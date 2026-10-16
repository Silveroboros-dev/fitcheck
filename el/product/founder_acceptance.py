"""Fixture-only case manifest for guided founder acceptance.

The manifest is presentation context, not semantic truth or a human decision.
It reuses checked-in fixture inputs and one synthetic privacy-screen input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from el.product.wiring import MULTI_THESIS_FIXTURE

FOUNDER_ACCEPTANCE_MANIFEST_VERSION = "v3.1-founder-acceptance-fixture-v2"

READY_SOURCE = (
    "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by "
    "the end of 2026."
)
NO_CLEAN_SOURCE = "Acme will buy a competitor in 2026."
PRIVACY_REFUSAL_SOURCE = (
    "Our confidential numbers show enterprise churn doubling."
)


@dataclass(frozen=True)
class FounderAcceptanceCase:
    case_id: str
    title: str
    source_text: str
    mission: str
    intake_context: str
    declared_coverage: tuple[str, ...]

    def public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["source_digest"] = hashlib.sha256(
            self.source_text.encode("utf-8")
        ).hexdigest()
        value["declared_coverage"] = list(self.declared_coverage)
        return value


FOUNDER_ACCEPTANCE_CASES = (
    FounderAcceptanceCase(
        case_id="FA-01",
        title="Stablecoin thesis has human priority",
        source_text=MULTI_THESIS_FIXTURE,
        mission=(
            "Choose the stablecoin candidate yourself. Review its clarification, "
            "the successor proposal, and the later market pool without treating "
            "fixture output as live-model evidence."
        ),
        intake_context=(
            "Shared source branch A of C. This is raw source text containing both "
            "claims. Run extraction first; then manually choose stablecoins."
        ),
        declared_coverage=(
            "two_source_candidates",
            "human_candidate_choice",
            "clarification_successor",
            "normalization_decision",
            "market_pool",
            "queued_reload_recovery",
        ),
    ),
    FounderAcceptanceCase(
        case_id="FA-02",
        title="Shared source branch B — choose Robinhood",
        source_text=MULTI_THESIS_FIXTURE,
        mission=(
            "Reload the same two-claim source, extract both candidates, then "
            "choose Robinhood yourself and judge its independent clarification "
            "and successor path."
        ),
        intake_context=(
            "Shared source branch B of C. The repeated two-claim text is "
            "intentional: it proves the Robinhood sibling remains available. "
            "Run extraction first; no thesis is preselected."
        ),
        declared_coverage=(
            "two_source_candidates",
            "different_human_priority",
            "clarification_successor",
            "normalization_decision",
            "market_pool",
        ),
    ),
    FounderAcceptanceCase(
        case_id="FA-03",
        title="Shared source branch C — choose neither",
        source_text=MULTI_THESIS_FIXTURE,
        mission=(
            "Reload the same two-claim source, extract both candidates, then "
            "choose the explicit source-level none option. Confirm that the "
            "product records the choice but creates no normalization proposal."
        ),
        intake_context=(
            "Shared source branch C of C. The repeated two-claim text is "
            "intentional: this branch tests rejecting both extracted candidates, "
            "not a different source."
        ),
        declared_coverage=(
            "two_source_candidates",
            "human_source_none",
            "no_normalization",
        ),
    ),
    FounderAcceptanceCase(
        case_id="FA-04",
        title="Ready thesis reaches up to three markets",
        source_text=READY_SOURCE,
        mission=(
            "Select the sole source candidate, decide on the ready normalization, "
            "then inspect each displayed market and make your own market-or-none "
            "choice."
        ),
        intake_context="Independent raw source for FA-04.",
        declared_coverage=(
            "single_source_candidate",
            "ready_normalization",
            "normalization_decision",
            "up_to_three_markets",
            "human_market_choice",
        ),
    ),
    FounderAcceptanceCase(
        case_id="FA-05",
        title="Scoped no-clean result",
        source_text=NO_CLEAN_SOURCE,
        mission=(
            "Accept the normalization only if it preserves your meaning, inspect "
            "the complete non-empty pool, and decide whether explicit market none "
            "is the honest useful action."
        ),
        intake_context="Independent raw source for FA-05.",
        declared_coverage=(
            "single_source_candidate",
            "normalization_decision",
            "scoped_no_clean_expression",
            "human_market_none",
        ),
    ),
    FounderAcceptanceCase(
        case_id="FA-06",
        title="Synthetic confidential-source refusal",
        source_text=PRIVACY_REFUSAL_SOURCE,
        mission=(
            "Confirm that the clearly synthetic confidential-source example is "
            "refused visibly and does not create a candidate or model call."
        ),
        intake_context="Independent synthetic confidential source for FA-06.",
        declared_coverage=(
            "privacy_refusal",
            "no_retained_raw_source",
            "no_model_call",
        ),
    ),
)


def founder_acceptance_manifest() -> list[dict[str, object]]:
    return [case.public_dict() for case in FOUNDER_ACCEPTANCE_CASES]


def founder_acceptance_manifest_digest() -> str:
    encoded = json.dumps(
        {
            "version": FOUNDER_ACCEPTANCE_MANIFEST_VERSION,
            "cases": founder_acceptance_manifest(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "FOUNDER_ACCEPTANCE_CASES",
    "FOUNDER_ACCEPTANCE_MANIFEST_VERSION",
    "NO_CLEAN_SOURCE",
    "PRIVACY_REFUSAL_SOURCE",
    "READY_SOURCE",
    "founder_acceptance_manifest",
    "founder_acceptance_manifest_digest",
]
