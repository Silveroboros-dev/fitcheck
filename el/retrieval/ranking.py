"""Deterministic retrieval ranking — ported from MFTA's proven scorer.

Lexical overlap + entity anchoring over candidate text fields. Two
deliberate divergences from MFTA, both recorded:

1. The query is built from the extracted structure (claim_summary,
   metric, stance, entity names) — never from raw input text.
2. MFTA's hardcoded IPO-context filter (`_has_ipo_context`) is dropped:
   it was a claim-text pattern branch, the family the NO-GO verdict
   retired. The retrieval-recall harness is the instrument that tells us
   whether dropping it costs recall; if it does, the fix is a governed
   ranking-policy change, not a resurrected text hack.

Determinism: stable float scoring, total ordering with market_id as the
final tiebreak — identical inputs always produce identical rankings
(run-invariance is a CI gate).
"""

import re

from el.domain.structures import ExtractedStructure
from el.retrieval.provider import CandidateMarketRecord

RANKING_POLICY_VERSION = "loop2-rank-v1"

_STOPWORDS = {
    "and", "are", "end", "for", "from", "has", "have", "into", "not",
    "the", "this", "that", "their", "will", "with", "unspecified",
}
_SHORT_ENTITY_ANCHORS = {"ai", "eu", "uk", "us"}
_NON_ENTITY_ANCHORS = {
    "if", "january", "jan", "february", "feb", "march", "mar", "april",
    "apr", "may", "june", "jun", "july", "jul", "august", "aug",
    "september", "sep", "sept", "october", "oct", "november", "nov",
    "december", "dec",
}


def rank_candidates(
    structure: ExtractedStructure,
    records: list[CandidateMarketRecord],
) -> list[tuple[CandidateMarketRecord, float]]:
    """Return (record, score) sorted best-first, totally ordered."""
    entities = [entity.name for entity in structure.entities]
    query_tokens = _tokens(
        " ".join(
            [
                structure.claim_summary,
                structure.metric.what,
                structure.stance.value,
                " ".join(entities),
            ]
        )
    )
    scored = []
    for record in records:
        haystack = _haystack(record)
        entity_matches = _entity_match_count(entities, haystack)
        score = _score(query_tokens, haystack, entity_matches)
        scored.append((record, score, entity_matches))
    scored.sort(
        key=lambda item: (
            -item[1],
            -item[2],
            -(item[0].liquidity_usd or 0.0),
            item[0].market_id,
        )
    )
    return [(record, score) for record, score, _ in scored]


def _score(
    query_tokens: set[str], haystack: str, entity_matches: int
) -> float:
    haystack_tokens = _tokens(haystack)
    query_semantic = {t for t in query_tokens if not t.isdigit()}
    overlap = {t for t in (query_tokens & haystack_tokens) if not t.isdigit()}
    overlap_score = len(overlap) / len(query_semantic) if query_semantic else 0.0
    if entity_matches == 0 and len(overlap) < 2:
        return 0.0
    return overlap_score + entity_matches * 0.08


def _haystack(record: CandidateMarketRecord) -> str:
    return " ".join(
        [
            record.title,
            record.description,
            record.resolution_rules,
            record.taxonomy_l1 or "",
            " ".join(record.tags),
        ]
    )


def _entity_match_count(entities: list[str], haystack: str) -> int:
    normalized_haystack = _normalize_entity_text(haystack)
    return sum(
        1
        for entity in entities
        if _is_entity_anchor(entity)
        and _entity_matches(entity, normalized_haystack)
    )


def _is_entity_anchor(entity: str) -> bool:
    normalized = _normalize_entity_text(entity)
    if not normalized or normalized in _NON_ENTITY_ANCHORS:
        return False
    if normalized in _STOPWORDS:
        return False
    if len(normalized) <= 2 and normalized not in _SHORT_ENTITY_ANCHORS:
        return False
    return True


def _entity_matches(entity: str, normalized_haystack: str) -> bool:
    cleaned = _normalize_entity_text(entity)
    if not cleaned:
        return False
    if re.search(
        rf"(?<![a-z0-9]){re.escape(cleaned)}(?![a-z0-9])", normalized_haystack
    ):
        return True
    # Multi-word entities still anchor when any distinctive word matches
    # ("Google Gemini" should anchor a "Gemini model" market title).
    words = [
        word
        for word in cleaned.split()
        if len(word) > 2 and word not in _STOPWORDS
    ]
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", normalized_haystack)
        for word in words
    )


def _normalize_entity_text(value: str) -> str:
    normalized = value.lower().replace("u.s.", "us")
    return re.sub(r"\s+", " ", normalized).strip()


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in _STOPWORDS
    }
