"""Governed metric-synonym aliases — METRIC-LEVEL equivalence (Loop 4 channel).

A metric alias version is a COMPLETE, frozen policy: its tokenization AND its
class table ship together and never change once promoted. Two versions are
governed here:

- alias-v1 (historical): stemmed token-set membership with a forbidden-token
  denylist. PROMOTED for rt_003 (sales~revenue), then found leaky: tok_v1
  stems "sales"->"sale", collapsing the revenue LINE ("sales", a flow) into a
  transaction ("sale"), and a denylist cannot enumerate every non-revenue use
  of "sale" — so "stake sale contraction", "divestiture sale contraction",
  "sale volume contraction" etc. cleared M1 against "revenue decline" and
  manufactured false-clean DIRECT expressions (external review, 2026-06-13).
  alias-v1 is kept byte-identical and reproducible; it is no longer the default.

- alias-v2 (current): UNSTEMMED, structural. A metric is in the revenue-change
  class iff it carries (a) a revenue term ("revenue"/"sales" — the plural flow,
  NEVER singular "sale"), AND (b) a change term ("contraction"/"decline"/...),
  AND (c) NO transaction/quantity disqualifier ("sale", "stake", "divestiture",
  "acquisition", "merger", "buyout", "volume", "unit", "price", "asset", ...).
  The singular/plural split is the mechanism the stemmed table destroyed:
  "sales" (revenue) is admitted; "sale" (a transaction) is excluded outright.

Equivalence holds only when BOTH metrics fall in the SAME class.

NOT laundering-safe by construction: clearing M1 removes a hard fail and raises
the ceiling. Safety = scoped class membership + adversarial negative tests +
the promotion verifier's before/after eval-delta (el.review.promotion). A new
class/version ships only through a recorded promotion. Pure, model-free,
deterministic.
"""

import re

from pydantic import BaseModel, ConfigDict

ALIAS_RULES_EMPTY = "alias-v0-empty"  # no classes; the verifier's "before"
ALIAS_RULES_V1 = "alias-v1"  # historical (leaky); kept reproducible, not default
ALIAS_RULES_VERSION = "alias-v2"  # current promoted default (leak closed)


class MetricAliasClass(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    # Each member is a set of tokens that must ALL be present for a metric to
    # match that phrasing.
    members: tuple[frozenset[str], ...]
    # If ANY of these tokens is present, the metric is EXCLUDED from the class
    # (it is a different kind of quantity that merely shares a surface word).
    forbidden: frozenset[str]


# --- alias-v1 (HISTORICAL — frozen, reproducible, no longer default) --------
# tok_v1 stems trailing-s for len>3 ("sales"->"sale", "assets"->"asset"). This
# is exactly why v1 leaks; preserved verbatim so the v1 GO artifact re-derives.
_REVENUE_CHANGE_V1 = MetricAliasClass(
    name="revenue_yoy_change",
    members=(
        frozenset({"sale", "contraction"}),
        frozenset({"sale", "decline"}),
        frozenset({"revenue", "contraction"}),
        frozenset({"revenue", "decline"}),
    ),
    forbidden=frozenset(
        {
            "asset", "company", "token", "price", "equity", "share",
            "property", "unit", "stock", "home", "house", "land",
        }
    ),
)

_ALIAS_TABLES_V1: dict[str, tuple[MetricAliasClass, ...]] = {
    ALIAS_RULES_V1: (_REVENUE_CHANGE_V1,),
    ALIAS_RULES_EMPTY: (),
}


def _in_class_v1(tokens: frozenset[str], cls: MetricAliasClass) -> bool:
    if tokens & cls.forbidden:
        return False
    return any(member <= tokens for member in cls.members)


def _equiv_v1(claim_text: str, market_text: str, version: str) -> bool:
    # Function-local import breaks the checks<->aliases cycle (checks imports
    # this module at load); v1 reproduces tok_v1's stemming EXACTLY by reusing
    # it, never a copy that could drift.
    from el.fitgate.checks import tok_v1

    claim_tokens, market_tokens = tok_v1(claim_text), tok_v1(market_text)
    for cls in _ALIAS_TABLES_V1.get(version, ()):
        if _in_class_v1(claim_tokens, cls) and _in_class_v1(market_tokens, cls):
            return True
    return False


# --- alias-v2 (CURRENT — unstemmed, structural) -----------------------------
# The revenue LINE is plural ("sales"/"revenue"); a singular "sale" is a
# transaction and is a disqualifier, not a synonym. NO stemming here — the
# singular/plural distinction is load-bearing and must survive.
_V2_REVENUE_TERMS = frozenset({"revenue", "revenues", "sales"})
_V2_CHANGE_TERMS = frozenset(
    {
        "contraction", "contractions", "decline", "declines", "decrease",
        "decreases", "drop", "drops", "fall", "falls", "shrink", "shrinks",
        "shrinkage", "contract", "contracts",
    }
)
# Quantities/events that merely share a surface word with the revenue line.
# Singular "sale" lives here (a transaction); "sales"/"revenue(s)" never do.
_V2_DISQUALIFIERS = frozenset(
    {
        "sale", "stake", "stakes", "divestiture", "divestitures",
        "acquisition", "acquisitions", "merger", "mergers", "buyout",
        "buyouts", "volume", "volumes", "unit", "units", "price", "prices",
        "asset", "assets", "equity", "equities", "share", "shares", "stock",
        "stocks", "token", "tokens", "company", "companies", "property",
        "properties", "disposal", "disposals", "transaction", "transactions",
        "land", "home", "homes", "house", "houses",
    }
)
_WORD_V2 = re.compile(r"[a-z0-9]+")


def _tok_raw(text: str | None) -> frozenset[str]:
    """Unstemmed content tokens: casefold + alphanumeric runs only. No plural
    stem — v2's whole point is that 'sales' (revenue) != 'sale' (transaction)."""
    if not text:
        return frozenset()
    return frozenset(_WORD_V2.findall(text.casefold()))


def _in_revenue_change_v2(tokens: frozenset[str]) -> bool:
    if tokens & _V2_DISQUALIFIERS:
        return False
    return bool(tokens & _V2_REVENUE_TERMS) and bool(tokens & _V2_CHANGE_TERMS)


def _equiv_v2(claim_text: str, market_text: str) -> bool:
    claim_tokens, market_tokens = _tok_raw(claim_text), _tok_raw(market_text)
    return _in_revenue_change_v2(claim_tokens) and _in_revenue_change_v2(
        market_tokens
    )


def metrics_alias_equivalent(
    claim_metric_text: str | None,
    market_metric_text: str | None,
    version: str = ALIAS_RULES_EMPTY,
) -> bool:
    """True iff both metrics fall in the SAME governed equivalence class.

    Takes RAW metric text (not pre-stemmed tokens) so each version owns its
    tokenization. The empty/unknown version has no classes -> always False
    (identity), so behavior is unchanged until a table is in force.
    """
    claim_metric_text = claim_metric_text or ""
    market_metric_text = market_metric_text or ""
    if version == ALIAS_RULES_VERSION:  # alias-v2 (current)
        return _equiv_v2(claim_metric_text, market_metric_text)
    if version in _ALIAS_TABLES_V1:  # alias-v1 (historical) or empty
        return _equiv_v1(claim_metric_text, market_metric_text, version)
    return False  # unknown version -> identity (no aliasing)
