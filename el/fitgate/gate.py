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
identity echo mismatch, restricted generated vocabulary, and an
INCOMPLETE condition set (an advisory must verify exactly the five named
conditions — a missing condition is missing evidence, not "no failure").
A rejected advisory never touches the verdict.

The predecessor also rejects restricted vocabulary in evidence fields.
The explicit source-quotes successor exempts only exact, source-verified
evidence spans and never relaxes generated-field filtering.

Predecessor quote-span (citation) enforcement lives in the CALLERS:
`quote_span_violations` needs the claim/rules source text, which the gate
normally does not see. FitService and the mode-3 harness run it against
captured texts. The source-quotes successor additionally requires source
corpora at the merge gate and rechecks its exemption there.
"""

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from el.domain.enums import FitClass
from el.domain.vocabulary import vocabulary_violations
from el.fitgate.checks import TOKEN_RULES_VERSION
from el.fitgate.token_rules import content_tokens, token_rules_version as policy_token_version
from el.fitgate.policy import (
    AUTHORITY_ADVISORY_VETO,
    AUTHORITY_DETERMINISTIC_ONLY,
    FitPolicy,
    MarketFitVerdict,
    class_rank,
    weaker_of,
)
from el.fitgate.runtime_policy import immutable_fit_policy
from el.models.fit_adapter import (
    REQUIRED_ADVISORY_CONDITIONS,
    FitAdvisory,
)

_WHITESPACE = re.compile(r"\s+")

# Historical all-field filtering stays the default. The owner-selected
# successor exempts only exact, source-verified evidence spans.
ADVISORY_VOCABULARY_POLICY_V1 = "fit-advisory-vocab-v1"
ADVISORY_SOURCE_QUOTES_POLICY_V2 = "fit-advisory-source-quotes-v2"


def validate_advisory_vocabulary_policy(version: str) -> None:
    if version not in {ADVISORY_VOCABULARY_POLICY_V1, ADVISORY_SOURCE_QUOTES_POLICY_V2}:
        raise ValueError(f"unsupported advisory vocabulary policy: {version}")


def _normalize(text: str) -> str:
    """Casefold + collapse whitespace, for forgiving substring matching."""
    return _WHITESPACE.sub(" ", text.casefold()).strip()


# An evidence span must carry at least this many content tokens (tok_v1),
# so a substring like "the" or a single common word cannot pass as a quote.
MIN_EVIDENCE_CONTENT_TOKENS = 2


def quote_span_violations(
    advisory: FitAdvisory,
    *,
    claim_text: str,
    rules_text: str = "",
    market_source_texts: Sequence[str] | None = None,
    token_rules_version: str = TOKEN_RULES_VERSION,
    exact: bool = False,
) -> list[str]:
    """Citation discipline (anti-mad-libs, blueprint Appendix A). Every
    condition's evidence must (1) be a verbatim quote of its source
    (normalized substring, or literal substring with exact=True) AND (2)
    carry >= MIN_EVIDENCE_CONTENT_TOKENS
    governed content tokens — so a stopword or a single common word that
    happens to appear in the source cannot launder as evidence.

    Returns the list of violations (empty = every span genuinely cites).
    ``market_source_texts`` keeps separately retained source fields separate:
    a quote must occur within one field and cannot cross their boundary.
    Source-text dependent, so it lives apart from the source-text-free
    merge gate; callers (FitService, mode-3) supply the corpora.
    """
    content_tokens(None, token_rules_version)
    claim_norm = claim_text if exact else _normalize(claim_text)
    market_sources = (
        tuple(market_source_texts)
        if market_source_texts is not None
        else (rules_text,)
    )
    normalized_market_sources = tuple(
        source if exact else _normalize(source) for source in market_sources
    )
    violations: list[str] = []
    for verdict in advisory.condition_verdicts:
        for label, evidence in (
            ("claim_evidence", verdict.claim_evidence),
            ("market_evidence", verdict.market_evidence),
        ):
            source = "the claim" if label == "claim_evidence" else "the market rules"
            span = evidence if exact else _normalize(evidence)
            corpora = (
                (claim_norm,)
                if label == "claim_evidence"
                else normalized_market_sources
            )
            if not any(span in corpus for corpus in corpora):
                violations.append(
                    f"{verdict.condition}: {label} is not a quote from {source}"
                )
            elif len(content_tokens(evidence, token_rules_version)) < MIN_EVIDENCE_CONTENT_TOKENS:
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


def _advisory_text_fields(advisory: FitAdvisory, *, include_evidence: bool = True) -> str:
    return " ".join(
        [
            advisory.what_it_captures,
            advisory.what_it_misses,
            advisory.falsifier,
            *advisory.bridge_assumptions,
            *([
                f"{v.claim_evidence} {v.market_evidence}"
                for v in advisory.condition_verdicts
            ] if include_evidence else []),
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
    policy: FitPolicy | None = None,
    vocabulary_policy_version: str = ADVISORY_VOCABULARY_POLICY_V1,
    claim_text: str | None = None,
    rules_text: str | None = None,
    market_source_texts: Sequence[str] | None = None,
    token_rules_version: str | None = None,
) -> AdvisoryMerge:
    """Produce the published verdict. Demotion-only by construction:
    published = min(ceiling, advisory-mapped class)."""
    validate_advisory_vocabulary_policy(vocabulary_policy_version)
    if deterministic.market_id != market_id:
        raise ValueError(
            "identity mismatch: deterministic verdict is for "
            f"{deterministic.market_id!r}, asked {market_id!r}"
        )
    if advisory is None:
        return AdvisoryMerge(verdict=deterministic, accepted=False)

    explicit_policy = policy is not None
    policy = immutable_fit_policy(policy)

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
    source_quotes = vocabulary_policy_version == ADVISORY_SOURCE_QUOTES_POLICY_V2
    if source_quotes:
        from el.fitgate.m1_subject_only import _bound_token_version

        expected_token_version = deterministic.token_rules_version
        valid_binding = True
        try:
            if expected_token_version != _bound_token_version(deterministic.gate_policy_version):
                raise ValueError("invalid deterministic token binding")
        except ValueError:
            valid_binding = False
            reasons.append("deterministic token binding is invalid")
        if token_rules_version is not None and token_rules_version != expected_token_version:
            reasons.append("quote token rules differ from deterministic fit policy")
        if explicit_policy and policy_token_version(policy) != expected_token_version:
            reasons.append("advisory policy token rules differ from deterministic fit policy")
        if claim_text is None or (
            rules_text is None and market_source_texts is None
        ):
            reasons.append("exact source quotes require bound claim and market source text")
        elif valid_binding:
            reasons.extend(quote_span_violations(
                advisory,
                claim_text=claim_text,
                rules_text=rules_text or "",
                market_source_texts=market_source_texts,
                token_rules_version=expected_token_version,
                exact=True,
            ))
    violations = vocabulary_violations(
        _advisory_text_fields(advisory, include_evidence=not source_quotes)
    )
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
