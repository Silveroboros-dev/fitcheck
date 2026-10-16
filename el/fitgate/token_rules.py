"""Explicit token-rule successor; historical ``tok_v1`` remains frozen.

The successor removes raw stopwords before the predecessor's stemming step.
Year removal, plural stemming, alias policy, direction guard, and thresholds
remain unchanged. Selecting it requires a named fit-policy successor.
"""

from el.domain.enums import FitClass
from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.checks import (
    CHANGE_DIRECTION_TOKENS,
    TOKEN_RULES_VERSION,
    CheckKind,
    CheckOutcome,
    CheckStatus,
    _STOPWORDS,
    _WORD,
    check_metric_lexical_floor as _metric_v1,
    check_subject_lexical_floor as _subject_v1,
    metrics_alias_equivalent,
    tok_v1,
)
from el.fitgate.aliases import ALIAS_RULES_EMPTY

TOKEN_RULES_SUCCESSOR_VERSION = "tok-v2-stopwords-before-stem"


def tok_v2(text: str | None) -> frozenset[str]:
    """Drop raw stopwords before applying all unchanged v1 token rules."""
    if not text:
        return frozenset()
    return tok_v1(
        " ".join(word for word in _WORD.findall(text.casefold()) if word not in _STOPWORDS)
    )


def content_tokens(
    text: str | None, version: str = TOKEN_RULES_VERSION
) -> frozenset[str]:
    if version == TOKEN_RULES_VERSION:
        return tok_v1(text)
    if version == TOKEN_RULES_SUCCESSOR_VERSION:
        return tok_v2(text)
    raise ValueError(f"unsupported token rules version: {version}")


def token_rules_version(policy: object) -> str:
    """Read explicit successor identity; unamended legacy policies use v1."""
    version = getattr(policy, "token_rules_version", TOKEN_RULES_VERSION)
    content_tokens(None, version)  # reject unsupported pins even for empty pools
    return version


def check_subject_lexical_floor(
    claim: ExtractedStructure,
    market: MarketStructure,
    version: str = TOKEN_RULES_VERSION,
) -> CheckOutcome:
    """The unchanged E1 rule over explicitly selected content tokens."""
    if version == TOKEN_RULES_VERSION:
        return _subject_v1(claim, market)
    content_tokens(None, version)
    subjects = [entity.name for entity in claim.entities if entity.role == "subject"]
    if not subjects:
        return _subject_v1(claim, market)
    market_names = [entity.name for entity in market.entities]
    market_tokens = set().union(*(content_tokens(name, version) for name in market_names))
    for subject in subjects:
        if content_tokens(subject, version) & market_tokens:
            return CheckOutcome(
                check_id="E1",
                name="subject_lexical_floor",
                kind=CheckKind.NONE,
                status=CheckStatus.PASS,
                detail=f"claim subject {subject!r} shares tokens with market entities {market_names}",
            )
    return CheckOutcome(
        check_id="E1",
        name="subject_lexical_floor",
        kind=CheckKind.HARD_CAP,
        status=CheckStatus.FAIL,
        cap=FitClass.WEAK_PROXY,
        hard=True,
        detail=(
            f"claim subject(s) {subjects} share no tokens with market "
            f"entities {market_names} ({version} floor; aliases arrive via Loop 4)"
        ),
    )


def check_metric_lexical_floor(
    claim: ExtractedStructure,
    market: MarketStructure,
    alias_version: str = ALIAS_RULES_EMPTY,
    direction_guard: bool = True,
    version: str = TOKEN_RULES_VERSION,
) -> CheckOutcome:
    """The unchanged M1 floor/alias rule over explicit content tokens."""
    if version == TOKEN_RULES_VERSION:
        return _metric_v1(claim, market, alias_version, direction_guard)
    claim_tokens = content_tokens(claim.metric.what, version)
    market_tokens = content_tokens(market.metric.what, version) | content_tokens(
        market.threshold, version
    )
    if not claim_tokens or not market_tokens:
        return CheckOutcome(
            check_id="M1",
            name="metric_lexical_floor",
            kind=CheckKind.NONE,
            status=CheckStatus.UNKNOWN,
            detail="a metric tokenized to nothing; floor cannot be established",
        )
    overlap = claim_tokens & market_tokens
    substantive_overlap = overlap - CHANGE_DIRECTION_TOKENS if direction_guard else overlap
    aliased = not substantive_overlap and metrics_alias_equivalent(
        claim.metric.what, f"{market.metric.what} {market.threshold or ''}", alias_version
    )
    if substantive_overlap or aliased:
        detail = (
            f"substantive overlap {sorted(substantive_overlap)[:5]}"
            if substantive_overlap
            else "governed metric-alias equivalence (same class)"
        ) + " — floor passed; metric identity NOT established"
        return CheckOutcome(
            check_id="M1",
            name="metric_lexical_floor",
            kind=CheckKind.NONE,
            status=CheckStatus.INCONCLUSIVE,
            detail=detail,
        )
    return CheckOutcome(
        check_id="M1",
        name="metric_lexical_floor",
        kind=CheckKind.HARD_CAP,
        status=CheckStatus.FAIL,
        cap=FitClass.WEAK_PROXY,
        hard=True,
        detail=(
            f"claim metric {claim.metric.what!r} shares no substantive tokens "
            f"with market metric {market.metric.what!r}"
            + (
                f" (overlap {sorted(overlap)} is direction/polarity only — not metric identity)"
                if overlap
                else f" ({version} floor)"
            )
            + "; synonyms arrive via Loop 4"
        ),
    )
