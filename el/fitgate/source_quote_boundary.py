"""Reverify successor evidence before exempting a FitCard source leaf."""

import hashlib
import uuid

from el.domain.tables import (
    CandidateSet,
    CandidateSetMember,
    FitCard,
    MarketRulesCapture,
    ThesisAnalysis,
)
from el.fitgate.gate import (
    ADVISORY_SOURCE_QUOTES_POLICY_V2,
    MIN_EVIDENCE_CONTENT_TOKENS,
)
from el.fitgate.m1_subject_only import _bound_token_version
from el.fitgate.token_rules import content_tokens


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bound_sources(
    session,
    *,
    card: FitCard,
    analysis: ThesisAnalysis,
    market_id: str,
    binding: dict,
) -> MarketRulesCapture | None:
    """Resolve only the capture belonging to this card's candidate graph."""
    if not isinstance(binding, dict):
        return None
    candidate_set = session.get(CandidateSet, card.candidate_set_id)
    if (
        candidate_set is None
        or candidate_set.thesis_analysis_id != card.thesis_analysis_id
        or binding.get("thesis_analysis_id") != str(card.thesis_analysis_id)
        or binding.get("candidate_set_id") != str(card.candidate_set_id)
        or binding.get("market_id") != market_id
        or binding.get("market_snapshot_id") != candidate_set.snapshot_id
    ):
        return None

    member_id = _uuid(binding.get("candidate_set_member_id"))
    member = session.get(CandidateSetMember, member_id) if member_id else None
    if (
        member is None
        or member.candidate_set_id != card.candidate_set_id
        or member.market_id != market_id
    ):
        return None

    capture_id = _uuid(binding.get("rules_capture_id"))
    capture = session.get(MarketRulesCapture, capture_id) if capture_id else None
    if (
        capture is None
        or capture.market_id != market_id
        or capture.snapshot_id != candidate_set.snapshot_id
    ):
        return None

    for text_field, hash_field in (
        ("contract_terms_text", "contract_terms_hash"),
        ("resolution_rules_text", "resolution_rules_hash"),
    ):
        actual = _sha256(getattr(capture, text_field))
        if (
            actual != getattr(capture, hash_field)
            or actual != binding.get(hash_field)
        ):
            return None
    if binding.get("claim_input_sha256") != _sha256(analysis.input_text):
        return None
    return capture


def verified_advisory_source_paths(payload: dict, session) -> frozenset[str]:
    """Only bound, field-local exact evidence in an accepted successor is source.

    Metadata declarations never grant an exemption on their own. Recheck
    each leaf against the retained raw input and the exact rules capture
    attached to this card's candidate set; leave every generated explanation
    subject to the ordinary A7 guard.
    """
    per_market = payload.get("provenance", {}).get("per_market", {})
    if not any(
        judgment.get("advisory", {}).get("vocabulary_policy_version")
        == ADVISORY_SOURCE_QUOTES_POLICY_V2
        for judgment in per_market.values()
    ):
        return frozenset()
    card_id = _uuid(payload.get("fit_card_id"))
    card = session.get(FitCard, card_id) if card_id else None
    if card is None or str(card.thesis_analysis_id) != str(
        payload.get("thesis_analysis_id")
    ):
        return frozenset()
    if payload.get("provenance") != card.provenance:
        return frozenset()
    analysis = session.get(ThesisAnalysis, card.thesis_analysis_id)
    if analysis is None:
        return frozenset()

    paths = set()
    for market_id, judgment in card.provenance.get("per_market", {}).items():
        metadata = judgment.get("advisory", {})
        if (
            metadata.get("status") != "accepted"
            or metadata.get("vocabulary_policy_version")
            != ADVISORY_SOURCE_QUOTES_POLICY_V2
        ):
            continue
        binding = metadata.get("source_binding", {})
        capture = _bound_sources(
            session,
            card=card,
            analysis=analysis,
            market_id=market_id,
            binding=binding,
        )
        if capture is None:
            continue

        failures = metadata.get("failures", [])
        version = binding.get("token_rules_version")
        if not failures:
            continue
        try:
            if (
                version != judgment.get("token_rules_version")
                or version
                != _bound_token_version(judgment.get("gate_policy_version"))
                or judgment.get("gate_policy_version")
                != card.provenance.get("gate_policy_version")
            ):
                continue
            valid = all(
                isinstance(failure.get("claim_evidence"), str)
                and failure["claim_evidence"] in analysis.input_text
                and len(content_tokens(failure["claim_evidence"], version))
                >= MIN_EVIDENCE_CONTENT_TOKENS
                and isinstance(failure.get("market_evidence"), str)
                and (
                    failure["market_evidence"] in capture.contract_terms_text
                    or failure["market_evidence"]
                    in capture.resolution_rules_text
                )
                and len(content_tokens(failure["market_evidence"], version))
                >= MIN_EVIDENCE_CONTENT_TOKENS
                for failure in failures
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if valid:
            for field in ("claim_evidence", "market_evidence"):
                paths.add(
                    f"provenance.per_market.{market_id}.advisory."
                    f"failures[].{field}"
                )
    return frozenset(paths)
