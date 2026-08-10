"""Loop 3 condition checks — pure functions over the two frozen structures.

Blueprint §14 (ratification item 9): every check compares
ExtractedStructure (claim side) to MarketStructure (contract side) —
never raw text, never model rationale. Checks come in four authority
types, and the type is explicit on every outcome (external-review
amendment: the set is not homogeneous and must not behave as if it
were):

- HARD_CAP: caps the class AND counts toward the no-clean stacking rule;
- SOFT_CAP: caps the class, never stacks;
- ANNOTATOR: never caps; produces card fields (risk, side);
- a PASS/INCONCLUSIVE outcome carries no authority at all.

M1 and E1 are lexical FLOORS, not identity checks: zero token overlap
demotes; overlap proves nothing and never blesses. Synonym misses cause
false hard fails — under-calls, the safe direction — and their product
cost is measured by the known_undercall_v1 red-team cases, not denied.
Alias/synonym tables arrive only via Loop 4 promotion with eval deltas.

This module must stay model-free: a CI structural check asserts no
model-adapter import can ever appear here.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from el.domain.enums import (
    FitClass,
    HorizonMatch,
    ResolutionRisk,
    ResolutionSourceClass,
    Stance,
)
from el.domain.structures import ExtractedStructure, MarketStructure
from el.fitgate.aliases import ALIAS_RULES_EMPTY, metrics_alias_equivalent

TOKEN_RULES_VERSION = "tok-v1"
# M1 floor-guard version (governed Step 5 / Loop 4 patch; before/after
# artifact: evals/promotions/m1_direction_token_floor_guard_v1.json).
M1_FLOOR_GUARD_VERSION = "m1-direction-guard-v1"

# Sources whose resolutions are mechanically checkable; a claim relying
# on one of these "demands objectivity" (M2's trigger condition).
_OBJECTIVE_SOURCES = frozenset(
    {
        ResolutionSourceClass.OFFICIAL,
        ResolutionSourceClass.LEADERBOARD,
        ResolutionSourceClass.FILING,
    }
)

# P1 direction vocabularies (governed sets; tok-v1-normalized membership).
_POSITIVE_DIRECTIONS = frozenset(
    {"above", "increase", "exceed", "rise", "higher", "over"}
)
_NEGATIVE_DIRECTIONS = frozenset(
    {"below", "decline", "decrease", "drop", "under", "lower"}
)
_POSITIVE_STANCES = frozenset({Stance.INCREASE, Stance.OUTPERFORM})
_NEGATIVE_STANCES = frozenset({Stance.DECREASE, Stance.UNDERPERFORM})

# M1 floor-guard (governed): change/direction/polarity tokens are NOT metric
# identity evidence. Two metrics both speaking of a "decline" share polarity,
# not subject — "market share decline" vs "revenue decline" is not a metric
# match. M1 subtracts these from the raw overlap before clearing the floor, so
# overlap on these alone no longer passes (the alias layer is the ONLY path
# that clears without substantive overlap). tok-v1-normalized membership.
CHANGE_DIRECTION_TOKENS = frozenset(
    {
        "decline", "declines", "decrease", "decreases", "drop", "drops",
        "fall", "falls", "increase", "increases", "rise", "rises", "growth",
        "grow", "grows", "contraction", "contractions", "contract",
        "contracts", "shrink", "shrinks", "above", "below", "higher",
        "lower", "over", "under",
    }
)

# Horizon tolerance table (loop3-v1 priors, governed policy data):
# claim precision -> (good_days, fair_days); beyond fair = poor.
# |market condition deadline - claim window_end| in days; the market
# date is the CONDITION DEADLINE under the blueprint §4 semantics pin.
HORIZON_TOLERANCES_V1: dict[str, tuple[int, int]] = {
    "day": (7, 14),
    "month": (31, 62),
    "quarter": (92, 184),
    "year": (366, 732),
}

# tok-v1: deliberately tiny and versioned — changing any of this is a
# policy change (Loop 4 + eval delta), not a refactor. "us" is NOT a
# stopword (US Congress must keep its token).
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "in", "on", "by", "for", "to", "or",
        "and", "with", "any", "at", "is", "be", "will", "its", "their",
        "from", "that", "this",
    }
)
_YEAR_TOKEN = re.compile(r"^(19|20)\d{2}$")
_WORD = re.compile(r"[a-z0-9]+")


def tok_v1(text: str | None) -> frozenset[str]:
    """Normalize text to comparable content tokens.

    casefold -> alphanumeric runs -> naive plural-s stem (len > 3) ->
    drop stopwords, bare year tokens (horizon info, not identity), and
    single-character alphabetic tokens ("Form S-1" must not leak an "s"
    that collides across unrelated metrics).
    """
    if not text:
        return frozenset()
    out: set[str] = set()
    for word in _WORD.findall(text.casefold()):
        if len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        if word in _STOPWORDS or _YEAR_TOKEN.match(word):
            continue
        if len(word) == 1 and word.isalpha():
            continue
        out.add(word)
    return frozenset(out)


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class CheckKind(StrEnum):
    HARD_CAP = "hard_cap"
    SOFT_CAP = "soft_cap"
    ANNOTATOR = "annotator"
    NONE = "none"


class CheckOutcome(BaseModel):
    """One condition check's recorded result.

    `cap` is the class ceiling this outcome imposes (None when it
    imposes nothing); `hard` marks outcomes that count toward the
    no-clean stacking rule. kind/cap/hard are redundant by construction
    (kind is the authority type of THIS outcome) — all three persist
    because rejection reasons and review triage read them directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    name: str
    kind: CheckKind
    status: CheckStatus
    cap: FitClass | None = None
    hard: bool = False
    detail: str
    # Annotator channel: card-bound fields (horizon_match, later
    # resolution_risk / thesis_side) ride outcomes; the policy layer
    # lifts known keys onto the verdict.
    annotations: dict[str, str] = {}


def check_horizon_tolerance(
    claim: ExtractedStructure,
    market: MarketStructure,
    tolerances: dict[str, tuple[int, int]] = HORIZON_TOLERANCES_V1,
) -> CheckOutcome:
    """H1 — horizon family: claim window vs market condition deadline.

    Tolerance scales with the claim's own precision (a year-precision
    claim is fuzzy by construction; a day-precision claim is not).
    good -> no cap; fair -> soft cap indirect (right test, degraded
    window); poor -> hard fail at weak_proxy. A market whose condition
    deadline precedes the claim's window_start is poor outright (it
    cannot observe the claim at all).
    """
    deadline = market.horizon.resolution_date
    window_start = claim.horizon.window_start
    good_days, fair_days = tolerances[claim.horizon.precision]

    if window_start and deadline < window_start:
        delta = (window_start - deadline).days
        grade, detail = HorizonMatch.POOR, (
            f"market condition deadline {deadline} precedes the claim "
            f"window start {window_start} by {delta}d — it cannot observe "
            "the claim"
        )
    else:
        delta = abs((deadline - claim.horizon.window_end).days)
        if delta <= good_days:
            grade = HorizonMatch.GOOD
        elif delta <= fair_days:
            grade = HorizonMatch.FAIR
        else:
            grade = HorizonMatch.POOR
        detail = (
            f"|condition deadline {deadline} - claim window end "
            f"{claim.horizon.window_end}| = {delta}d at precision "
            f"{claim.horizon.precision} (good <= {good_days}d, fair <= "
            f"{fair_days}d)"
        )

    annotations = {"horizon_match": grade.value}
    if grade is HorizonMatch.GOOD:
        return CheckOutcome(
            check_id="H1",
            name="horizon_tolerance",
            kind=CheckKind.ANNOTATOR,
            status=CheckStatus.PASS,
            detail=detail,
            annotations=annotations,
        )
    if grade is HorizonMatch.FAIR:
        return CheckOutcome(
            check_id="H1",
            name="horizon_tolerance",
            kind=CheckKind.SOFT_CAP,
            status=CheckStatus.FAIL,
            cap=FitClass.INDIRECT,
            detail=detail,
            annotations=annotations,
        )
    return CheckOutcome(
        check_id="H1",
        name="horizon_tolerance",
        kind=CheckKind.HARD_CAP,
        status=CheckStatus.FAIL,
        cap=FitClass.WEAK_PROXY,
        hard=True,
        detail=detail,
        annotations=annotations,
    )


def check_event_stage_match(
    claim: ExtractedStructure, market: MarketStructure
) -> CheckOutcome:
    """S1 — the es_ stress family, now an enum comparison.

    Any mismatch is a hard fail at weak_proxy, uniformly: stage
    adjacency was rejected at ratification because it does not encode
    trap severity (announced≠launched is the GPT-5 trap; adopted≠measured
    is law-passed-vs-effect-realized — both are distance 1).
    """
    if claim.event_stage == market.event_stage:
        return CheckOutcome(
            check_id="S1",
            name="event_stage_match",
            kind=CheckKind.NONE,
            status=CheckStatus.PASS,
            detail=f"claim and market resolve on the same stage "
            f"({claim.event_stage})",
        )
    return CheckOutcome(
        check_id="S1",
        name="event_stage_match",
        kind=CheckKind.HARD_CAP,
        status=CheckStatus.FAIL,
        cap=FitClass.WEAK_PROXY,
        hard=True,
        detail=(
            f"claim asserts stage {claim.event_stage}; market resolves on "
            f"stage {market.event_stage} — the market can resolve YES while "
            "the claim stays false"
        ),
    )


def _claim_demands_objectivity(claim: ExtractedStructure) -> bool:
    return (
        claim.metric.objective
        and claim.resolution_source_class in _OBJECTIVE_SOURCES
    )


def check_objectivity_conflict(
    claim: ExtractedStructure, market: MarketStructure
) -> CheckOutcome:
    """M2 — metric family: objective claim, subjective resolution.

    Fires only when the claim DEMANDS objective resolution (its own
    metric is objective AND it relies on an official/leaderboard/filing
    source). A press-sourced claim cannot demand more objectivity than
    its own source class provides (the eval_005 lesson: Apple-keynote
    claims tolerate judgment-resolved markets; the risk shows on R1,
    not as a fit refusal).
    """
    if _claim_demands_objectivity(claim) and not market.metric.objective:
        return CheckOutcome(
            check_id="M2",
            name="objectivity_conflict",
            kind=CheckKind.HARD_CAP,
            status=CheckStatus.FAIL,
            cap=FitClass.WEAK_PROXY,
            hard=True,
            detail=(
                "claim demands objective resolution "
                f"(source {claim.resolution_source_class}) but the market "
                "resolves by judgment call — a confounded grader for a "
                "crisp claim"
            ),
        )
    return CheckOutcome(
        check_id="M2",
        name="objectivity_conflict",
        kind=CheckKind.NONE,
        status=CheckStatus.PASS,
        detail="no objectivity conflict",
    )


def check_objectivity_skew(
    claim: ExtractedStructure, market: MarketStructure
) -> CheckOutcome:
    """M3 — metric family: subjective claim, objective market.

    An objective market can strongly evidence a fuzzy claim but never
    distinguish it (the market's crisp threshold is not the claim's
    fuzzy proposition). Soft cap at indirect.
    """
    if not claim.metric.objective and market.metric.objective:
        return CheckOutcome(
            check_id="M3",
            name="objectivity_skew",
            kind=CheckKind.SOFT_CAP,
            status=CheckStatus.FAIL,
            cap=FitClass.INDIRECT,
            detail=(
                "claim metric is subjective; the market resolves a crisp "
                "threshold — strong evidence at best, never a "
                "distinguishing test"
            ),
        )
    return CheckOutcome(
        check_id="M3",
        name="objectivity_skew",
        kind=CheckKind.NONE,
        status=CheckStatus.PASS,
        detail="no objectivity skew",
    )


def check_resolution_source_risk(
    claim: ExtractedStructure, market: MarketStructure
) -> CheckOutcome:
    """R1 — annotator: how reliably will this market's resolution grade
    the test? Never caps the class (fit-of-meaning and grader quality
    are different axes; the card shows both).
    """
    subjective = not market.metric.objective
    weak_source = market.resolution_source_class in (
        ResolutionSourceClass.PRESS,
        ResolutionSourceClass.NONE,
    )
    conflict = _claim_demands_objectivity(claim) and subjective
    if (
        market.resolution_source_class is ResolutionSourceClass.NONE
        or (subjective and weak_source)
        or conflict
    ):
        risk = ResolutionRisk.HIGH
    elif subjective or weak_source:
        risk = ResolutionRisk.MEDIUM
    else:
        risk = ResolutionRisk.LOW
    return CheckOutcome(
        check_id="R1",
        name="resolution_source_risk",
        kind=CheckKind.ANNOTATOR,
        status=CheckStatus.PASS,
        detail=(
            f"market resolves via {market.resolution_source_class} "
            f"({'judgment-based' if subjective else 'objective'}) -> "
            f"resolution risk {risk}"
        ),
        annotations={"resolution_risk": risk.value},
    )


def check_outcome_polarity(
    claim: ExtractedStructure, market: MarketStructure
) -> CheckOutcome:
    """P1 — outcome polarity (external-review amendment): WHICH market
    outcome does the thesis map to?

    Side never blocks expression on a binary market — it determines
    what the ledger records. A directional stance against an absent or
    unrecognized market direction leaves the side unresolved: flagged
    and soft-capped at indirect (intent cannot be cleanly recorded).
    Stance `unclear` only flags — claim-side ambiguity is Loop 1's
    surface, and the fit class is market-relative.
    """
    stance = claim.stance
    if stance is Stance.YES:
        side, detail = "yes", "thesis affirms the market question as framed"
    elif stance is Stance.NO:
        side, detail = "no", (
            "thesis negates the market question — it maps to the NO outcome"
        )
    elif stance is Stance.UNCLEAR:
        return CheckOutcome(
            check_id="P1",
            name="outcome_polarity",
            kind=CheckKind.ANNOTATOR,
            status=CheckStatus.UNKNOWN,
            detail=(
                "claim stance is unclear; side unresolved — Loop 1 "
                "ambiguity surface, not a fit refusal"
            ),
            annotations={"thesis_side": "unknown"},
        )
    else:
        direction_tokens = tok_v1(market.direction)
        positive = bool(direction_tokens & _POSITIVE_DIRECTIONS)
        negative = bool(direction_tokens & _NEGATIVE_DIRECTIONS)
        stance_positive = stance in _POSITIVE_STANCES
        if positive != negative:  # exactly one direction recognized
            aligned = (positive and stance_positive) or (
                negative and not stance_positive
            )
            side = "yes" if aligned else "no"
            detail = (
                f"directional stance {stance} vs market direction "
                f"{market.direction!r} -> thesis maps to the "
                f"{side.upper()} outcome"
            )
        else:
            return CheckOutcome(
                check_id="P1",
                name="outcome_polarity",
                kind=CheckKind.SOFT_CAP,
                status=CheckStatus.UNKNOWN,
                cap=FitClass.INDIRECT,
                detail=(
                    f"outcome_side_unresolved: directional stance {stance} "
                    f"but market direction {market.direction!r} is absent "
                    "or unrecognized — intent cannot be cleanly recorded"
                ),
                annotations={"thesis_side": "unknown"},
            )
    return CheckOutcome(
        check_id="P1",
        name="outcome_polarity",
        kind=CheckKind.ANNOTATOR,
        status=CheckStatus.PASS,
        detail=detail,
        annotations={"thesis_side": side},
    )


def check_mechanism_endpoint(
    claim: ExtractedStructure, market: MarketStructure
) -> CheckOutcome:
    """X1 — causal_mechanism family, claim-side trigger.

    When the claim asserts a causal chain ("Y because M"), no single
    market can distinguish mechanism + endpoint: an endpoint market
    confirms Y without M, a mechanism market confirms M without Y.
    Soft cap at indirect for EVERY market — strong evidence is the
    honest maximum for a causal claim (eval_009 is the seed proof).
    """
    chain = claim.mechanism.asserted_causal_chain
    if chain is None:
        return CheckOutcome(
            check_id="X1",
            name="mechanism_endpoint",
            kind=CheckKind.NONE,
            status=CheckStatus.NOT_APPLICABLE,
            detail="claim asserts no causal mechanism",
        )
    return CheckOutcome(
        check_id="X1",
        name="mechanism_endpoint",
        kind=CheckKind.SOFT_CAP,
        status=CheckStatus.FAIL,
        cap=FitClass.INDIRECT,
        detail=(
            f"claim asserts a causal chain ({chain!r}); a market resolves "
            "an outcome, not a mechanism — no single market is a "
            "distinguishing test of the causal claim"
        ),
    )


def check_subject_lexical_floor(
    claim: ExtractedStructure, market: MarketStructure
) -> CheckOutcome:
    """E1 — the bad-twin guard, claim subject vs market entities.

    ANY subject-role entity sharing a token with ANY market entity
    passes (multi-subject composites are X2's job). Zero overlap across
    all pairs is a hard fail. No subject-role entity at all is UNKNOWN:
    flagged, capped soft, never guessed (the Loop 2 precedent).
    """
    subjects = [e.name for e in claim.entities if e.role == "subject"]
    if not subjects:
        return CheckOutcome(
            check_id="E1",
            name="subject_lexical_floor",
            kind=CheckKind.SOFT_CAP,
            status=CheckStatus.UNKNOWN,
            cap=FitClass.INDIRECT,
            detail=(
                "no subject-role entity extracted; subject identity "
                "cannot be established — flagged, never guessed"
            ),
        )
    market_names = [e.name for e in market.entities]
    market_token_sets = [tok_v1(name) for name in market_names]
    for subject in subjects:
        subject_tokens = tok_v1(subject)
        for market_tokens in market_token_sets:
            if subject_tokens & market_tokens:
                return CheckOutcome(
                    check_id="E1",
                    name="subject_lexical_floor",
                    kind=CheckKind.NONE,
                    status=CheckStatus.PASS,
                    detail=(
                        f"claim subject {subject!r} shares tokens with "
                        f"market entities {market_names}"
                    ),
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
            f"entities {market_names} (tok-v1 floor; aliases arrive via "
            "Loop 4)"
        ),
    )


def check_metric_lexical_floor(
    claim: ExtractedStructure,
    market: MarketStructure,
    alias_version: str = ALIAS_RULES_EMPTY,
    direction_guard: bool = True,
) -> CheckOutcome:
    """M1 — metric lexical floor: claim metric vs market metric+threshold.

    Zero content-token overlap between non-empty token sets proves the
    metrics are lexically disjoint -> hard fail. Overlap establishes
    NOTHING (a floor, not an identity check): the outcome is
    INCONCLUSIVE, never PASS, so nothing downstream can read it as
    blessing the metric match.

    When raw tokens do not overlap, the governed alias layer
    (el.fitgate.aliases) is consulted on the RAW metric text — it owns its
    own (version-specific) tokenization, so promoted synonyms (e.g.
    sales~revenue under revenue-decline context) clear the floor. The
    empty-token guard above still runs on tok_v1 tokens (an alias cannot
    conjure a floor from nothing); alias_version defaults to the identity, so
    behavior is unchanged until the verifier promotes a table.

    Direction-token guard (m1-direction-guard-v1, governed): change/polarity
    tokens (CHANGE_DIRECTION_TOKENS — decline, increase, drop, ...) are
    subtracted from the raw overlap before the floor clears. Sharing only
    "decline" is shared polarity, not metric identity ("market share decline"
    vs "revenue decline"), so it no longer passes; the alias layer is then the
    only path that clears without substantive overlap. direction_guard=False
    is the verifier's before-baseline.
    """
    claim_tokens = tok_v1(claim.metric.what)
    market_tokens = tok_v1(market.metric.what) | tok_v1(market.threshold)
    if not claim_tokens or not market_tokens:
        return CheckOutcome(
            check_id="M1",
            name="metric_lexical_floor",
            kind=CheckKind.NONE,
            status=CheckStatus.UNKNOWN,
            detail="a metric tokenized to nothing; floor cannot be established",
        )
    overlap = claim_tokens & market_tokens
    substantive_overlap = (
        overlap - CHANGE_DIRECTION_TOKENS if direction_guard else overlap
    )
    aliased = not substantive_overlap and metrics_alias_equivalent(
        claim.metric.what,
        f"{market.metric.what} {market.threshold or ''}",
        alias_version,
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
                f" (overlap {sorted(overlap)} is direction/polarity only — "
                "not metric identity)"
                if overlap
                else " (tok-v1 floor)"
            )
            + "; synonyms arrive via Loop 4"
        ),
    )


def check_composite_single_leg(
    claim: ExtractedStructure,
    market: MarketStructure,
    composite_coverage=None,
    alias_rules_version: str = ALIAS_RULES_EMPTY,
    m1_direction_guard: bool = True,
    horizon_tolerances: dict | None = None,
) -> CheckOutcome:
    """X2 — composite_thesis family, claim-side trigger.

    A composite (conjunctive) claim has no single-market distinguishing
    test under schema v1: a leg-market is necessary-not-sufficient
    evidence, and v1 cannot verify full-leg coverage deterministically
    (no leg structure; the single stage field collapses leg stages).
    Soft cap at indirect for ALL markets. The over-refusal on genuine
    full-cover composite markets is MEASURED by rt_004 (known_undercall_v1),
    not denied; structured legs are a schema-v2 candidate via Loop 4.
    """
    if not claim.mechanism.is_composite:
        return CheckOutcome(
            check_id="X2",
            name="composite_single_leg",
            kind=CheckKind.NONE,
            status=CheckStatus.NOT_APPLICABLE,
            detail="claim is not composite",
        )
    if composite_coverage is not None:
        from el.fitgate.composite_v2 import evaluate_composite_coverage

        result = evaluate_composite_coverage(
            claim,
            market,
            composite_coverage,
            alias_rules_version=alias_rules_version,
            m1_direction_guard=m1_direction_guard,
            horizon_tolerances=horizon_tolerances,
        )
        if result.status == "full_cover":
            return CheckOutcome(
                check_id="X2",
                name="composite_single_leg",
                kind=CheckKind.NONE,
                status=CheckStatus.PASS,
                detail=result.detail,
            )
        if result.status == "not_full_cover":
            return CheckOutcome(
                check_id="X2",
                name="composite_single_leg",
                kind=CheckKind.SOFT_CAP,
                status=CheckStatus.FAIL,
                cap=FitClass.INDIRECT,
                detail=f"composite-v2 coverage rejected direct: {result.detail}",
            )
    return CheckOutcome(
        check_id="X2",
        name="composite_single_leg",
        kind=CheckKind.SOFT_CAP,
        status=CheckStatus.FAIL,
        cap=FitClass.INDIRECT,
        detail=(
            "composite thesis: no single market distinguishes a "
            "conjunction; a market covering one leg is strong evidence "
            "at best, and v1 cannot verify full-leg coverage"
        ),
    )


# Registry the policy engine runs, in build order. One stress family per
# commit: a check lands here WITH its harness gating or not at all.
ALL_CHECKS = (
    check_event_stage_match,
    check_subject_lexical_floor,
    check_metric_lexical_floor,
    check_objectivity_conflict,
    check_objectivity_skew,
    check_horizon_tolerance,
    check_mechanism_endpoint,
    check_composite_single_leg,
    check_resolution_source_risk,
    check_outcome_polarity,
)
