"""Loop 3 advisory gate — demotion-only merge (blueprint §14).

Authority split, enforced here:

    deterministic_ceiling = pure policy output (already computed)
    advisory_caps         = advisory condition FAILS mapped through this
                            module's cap table — the same shape as the
                            deterministic mapping, including stacking
    published_fit_class   = min(ceiling, advisory_caps)

The advisory's suggested_class is NEVER read by the class computation —
it is recorded for disagreement telemetry only. A hostile or
sycophantic advisory therefore cannot raise any class; the worst it can
do is demote (the safe direction) or get rejected.

Rejection criteria (advisory discarded, caller retries or falls back):
identity echo mismatch, restricted vocabulary in any text field, and an
INCOMPLETE condition set (an advisory must verify exactly the five named
conditions — a missing condition is missing evidence, not "no failure").
A rejected advisory never touches the verdict.

Quote-span (citation) enforcement lives in the CALLERS, not here:
`quote_span_violations` needs the claim/rules source text, which the gate
(by design) never sees. FitService and the mode-3 harness run it against
the captured texts and reject non-citing advisories before/around the
merge.
"""

import re

from pydantic import BaseModel, ConfigDict

from el.domain.enums import FitClass
from el.domain.vocabulary import vocabulary_violations
from el.fitgate.checks import tok_v1
from el.fitgate.policy import (
    AUTHORITY_ADVISORY_VETO,
    AUTHORITY_DETERMINISTIC_ONLY,
    FitPolicy,
    MarketFitVerdict,
    class_rank,
    weaker_of,
)
from el.models.fit_adapter import (
    REQUIRED_ADVISORY_CONDITIONS,
    FitAdvisory,
)

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Casefold + collapse whitespace, for forgiving substring matching."""
    return _WHITESPACE.sub(" ", text.casefold()).strip()


# An evidence span must carry at least this many content tokens (tok_v1),
# so a substring like "the" or a single common word cannot pass as a quote.
MIN_EVIDENCE_CONTENT_TOKENS = 2


def quote_span_violations(
    advisory: FitAdvisory, *, claim_text: str, rules_text: str
) -> list[str]:
    """Citation discipline (anti-mad-libs, blueprint Appendix A). Every
    condition's evidence must (1) be a verbatim quote of its source
    (normalized substring) AND (2) carry >= MIN_EVIDENCE_CONTENT_TOKENS
    governed content tokens — so a stopword or a single common word that
    happens to appear in the source cannot launder as evidence.

    Returns the list of violations (empty = every span genuinely cites).
    Source-text dependent, so it lives apart from the source-text-free
    merge gate; callers (FitService, mode-3) supply the corpora.
    """
    claim_norm = _normalize(claim_text)
    rules_norm = _normalize(rules_text)
    violations: list[str] = []
    for verdict in advisory.condition_verdicts:
        for label, evidence, corpus in (
            ("claim_evidence", verdict.claim_evidence, claim_norm),
            ("market_evidence", verdict.market_evidence, rules_norm),
        ):
            source = "the claim" if label == "claim_evidence" else "the market rules"
            if _normalize(evidence) not in corpus:
                violations.append(
                    f"{verdict.condition}: {label} is not a quote from {source}"
                )
            elif len(tok_v1(evidence)) < MIN_EVIDENCE_CONTENT_TOKENS:
                violations.append(
                    f"{verdict.condition}: {label} is too thin "
                    f"(< {MIN_EVIDENCE_CONTENT_TOKENS} content tokens) — "
                    "a stopword or single word is not a citation"
                )
    return violations

# Advisory condition -> (cap on fail, counts toward stacking). The same
# severity semantics as the deterministic checks: identity/metric/stage/
# resolution failures are hard; horizon coverage alone is soft.
ADVISORY_CAP_TABLE: dict[str, tuple[FitClass, bool]] = {
    "same_event_stage": (FitClass.WEAK_PROXY, True),
    "same_metric": (FitClass.WEAK_PROXY, True),
    "horizon_covers_claim": (FitClass.INDIRECT, False),
    "subject_is_claim_subject": (FitClass.WEAK_PROXY, True),
    "resolution_observes_truth_conditions": (FitClass.WEAK_PROXY, True),
}


class AdvisoryMerge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: MarketFitVerdict
    accepted: bool
    rejected_reasons: list[str] = []
    advisory_class: FitClass | None = None
    suggested_class: FitClass | None = None
    disagreement: bool = False


def _advisory_text_fields(advisory: FitAdvisory) -> str:
    return " ".join(
        [
            advisory.what_it_captures,
            advisory.what_it_misses,
            advisory.falsifier,
            *advisory.bridge_assumptions,
            *[
                f"{v.claim_evidence} {v.market_evidence}"
                for v in advisory.condition_verdicts
            ],
        ]
    )


def advisory_class_from_verdicts(
    advisory: FitAdvisory, policy: FitPolicy
) -> FitClass:
    """Map advisory condition FAILS through the cap table — the same
    cap-and-stack shape as the deterministic ceiling."""
    advisory_class = FitClass.DIRECT
    hard_fails = 0
    for verdict in advisory.condition_verdicts:
        if verdict.status != "fail":
            continue
        cap, hard = ADVISORY_CAP_TABLE[verdict.condition]
        advisory_class = weaker_of(advisory_class, cap)
        if hard:
            hard_fails += 1
    if hard_fails >= policy.stacking_threshold:
        advisory_class = weaker_of(
            advisory_class, FitClass.NO_CLEAN_EXPRESSION
        )
    return advisory_class


def merge_advisory(
    deterministic: MarketFitVerdict,
    advisory: FitAdvisory | None,
    *,
    market_id: str,
    policy: FitPolicy = FitPolicy(),
) -> AdvisoryMerge:
    """Produce the published verdict. Demotion-only by construction:
    published = min(ceiling, advisory-mapped class)."""
    if advisory is None:
        return AdvisoryMerge(verdict=deterministic, accepted=False)

    reasons: list[str] = []
    if advisory.market_id != market_id:
        reasons.append(
            f"identity mismatch: advisory answered for "
            f"{advisory.market_id!r}, asked {market_id!r}"
        )
    present = {verdict.condition for verdict in advisory.condition_verdicts}
    if present != REQUIRED_ADVISORY_CONDITIONS:
        missing = sorted(REQUIRED_ADVISORY_CONDITIONS - present)
        reasons.append(
            f"incomplete advisory: did not verify {missing} — a missing "
            "condition is missing evidence, not a pass"
        )
    violations = vocabulary_violations(_advisory_text_fields(advisory))
    if violations:
        reasons.append(f"restricted vocabulary: {', '.join(violations)}")
    if reasons:
        return AdvisoryMerge(
            verdict=deterministic, accepted=False, rejected_reasons=reasons
        )

    advisory_class = advisory_class_from_verdicts(advisory, policy)
    published = weaker_of(deterministic.deterministic_ceiling, advisory_class)
    vetoed = class_rank(published) < class_rank(
        deterministic.deterministic_ceiling
    )
    merged = deterministic.model_copy(
        update={
            "published": published,
            "authority": (
                AUTHORITY_ADVISORY_VETO
                if vetoed
                else AUTHORITY_DETERMINISTIC_ONLY
            ),
        }
    )
    return AdvisoryMerge(
        verdict=merged,
        accepted=True,
        advisory_class=advisory_class,
        suggested_class=advisory.suggested_class,
        disagreement=advisory.suggested_class != published,
    )
