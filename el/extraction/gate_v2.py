"""Loop 1 v2 gate, isolated from the frozen historical v1 evaluator."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from el.domain.enums import EventStage, Stance
from el.domain.structures import ExtractedStructure
from el.domain.vocabulary import vocabulary_violations
from el.extraction.gate import GateVerdict
from el.models.adapter import ExtractionProposal
from el.models.adapter_v2 import ExtractionProposalV2, ProposalModeV2


GATE_POLICY_VERSION = "loop1-v2"
_FALLBACK_QUESTION = 'Which specific, checkable outcome do you mean by: "{summary}"?'

_INSIDER_RELIANCE = re.compile(
    r"(?:"
    r"\b(?:i|we)\b\s+(?:have|know)\s+"
    r"(?:(?:access to|knowledge of)\s+)?(?:insider (?:info|information|knowledge)"
    r"|mnpi|internal memo|leaked internal(?: memo)?"
    r"|confidential (?:data|figures|numbers|deck|document))\b"
    r"|"
    r"\b(?:i|we)\b.{0,24}\b(?:possess|received|learned|saw|use|using|"
    r"rely on|relying on)\b.{0,32}\b(?:insider (?:info|information|knowledge)"
    r"|mnpi|internal memo|leaked internal(?: memo)?"
    r"|confidential (?:data|figures|numbers|deck|document))\b"
    r"|\b(?:i am|we are)\s+under nda\b"
    r"|\b(?:my|our)\s+(?:insider (?:info|information|knowledge)|mnpi|"
    r"confidential (?:data|figures|numbers|deck|document))\b.{0,24}"
    r"\b(?:show|indicate|suggest|confirm|reveal|support|inform)\b"
    r"|\b(?:my|our)\s+(?:employer|company|client)'s\s+"
    r"(?:private|internal|confidential)\s+"
    r"(?:data|figures|numbers|memo|deck|document)\b.{0,24}"
    r"\b(?:show|indicate|suggest|confirm|reveal|support|inform)\b"
    r"|\b(?:based on|using|relying on)\s+(?:my |our )?(?:"
    r"insider (?:info|information)|mnpi|internal memo|leaked internal(?: memo)?"
    r"|confidential (?:data|figures|numbers|deck|document))\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_NONPUBLIC_RELIANCE = re.compile(
    r"(?:"
    r"\b(?:i|we)\b\s+(?:have|know|possess|received|learned|saw|use|using)\s+"
    r"(?:(?:access to|knowledge of)\s+)?\bnon[- ]?public information\b"
    r"|\b(?:i am|we are)\s+(?:using|relying on)\s+"
    r"\bnon[- ]?public information\b"
    r"|\b(?:my|our)\b.{0,32}\b(?:claim|analysis|view|thesis)\b.{0,24}"
    r"\b(?:uses?|using|based on|relies on)\b.{0,16}"
    r"\bnon[- ]?public information\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_AMBIGUITY_TAG = re.compile(r"^\s*(blocking|sibling)\s*:\s*(.+?)\s*$", re.I)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_FORECAST_ATTRIBUTION = re.compile(
    r"\b(?:forecast(?:s|ed|ing)?|guidance|project(?:s|ed|ing)?|"
    r"expect(?:s|ed|ing)?|anticipat(?:e|es|ed|ing)|target(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)
_REALIZED_ATTRIBUTION = re.compile(
    r"\b(?:already|achieved|reported actual|realized|completed)\b", re.I
)
_FUTURE_MODAL = re.compile(r"\b(?:will|would|expected|forecast|projected)\b", re.I)
_LOWER_BOUND = re.compile(
    r"\b(?:not before|no earlier than|on or after)\b", re.IGNORECASE
)
_UPPER_BOUND = re.compile(
    r"\b(?:by|no later than|on or before)\b", re.IGNORECASE
)
_AMBIGUITY_PREFIX = re.compile(r"^\s*(?:blocking|sibling)\s*:", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"(?:[.!?](?=\s|$)|[\r\n]+)")


class Loop1V2Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: GateVerdict
    structure: ExtractedStructure | None = None
    clarifying_question: str | None = None
    blocking_ambiguities: list[str] = Field(default_factory=list)
    sibling_claims: list[str] = Field(default_factory=list)
    legacy_unclassified_ambiguities: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    gate_policy_version: Literal["loop1-v2"] = GATE_POLICY_VERSION


def insider_screen(input_text: str) -> bool:
    """Refuse explicit possession/reliance, not public boilerplate alone."""

    return bool(
        _INSIDER_RELIANCE.search(input_text)
        or _NONPUBLIC_RELIANCE.search(input_text)
    )


def _partition_ambiguities(
    values: Iterable[str],
) -> tuple[list[str], list[str], list[str]]:
    blocking: list[str] = []
    siblings: list[str] = []
    unclassified: list[str] = []
    for value in values:
        match = _AMBIGUITY_TAG.fullmatch(value)
        if match is None:
            unclassified.append(value)
            continue
        destination = (
            blocking if match.group(1).casefold() == "blocking" else siblings
        )
        destination.append(match.group(2).strip())
    return blocking, siblings, unclassified


def _grounding_failures(input_text: str, structure: ExtractedStructure) -> list[str]:
    failures: list[str] = []
    normalized = f"{structure.claim_summary} {structure.contractible_version}"
    source_years = set(_YEAR.findall(input_text))
    normalized_years = set(_YEAR.findall(normalized))
    if (
        str(structure.horizon.window_end.year) not in source_years
        or not normalized_years.issubset(source_years)
    ):
        failures.append("horizon_year_not_grounded_in_source")

    if (
        _LOWER_BOUND.search(input_text)
        and _LOWER_BOUND.search(structure.claim_summary)
        and not _LOWER_BOUND.search(structure.contractible_version)
    ):
        failures.append("lower_bound_changed_in_contractible_version")

    source_forecast = bool(_FORECAST_ATTRIBUTION.search(input_text))
    summary_forecast = bool(_FORECAST_ATTRIBUTION.search(structure.claim_summary))
    contract_forecast = bool(
        _FORECAST_ATTRIBUTION.search(structure.contractible_version)
    )
    if source_forecast and summary_forecast and not contract_forecast:
        failures.append("forecast_attribution_not_preserved")
    if (
        source_forecast
        and summary_forecast
        and structure.event_stage is not EventStage.ANNOUNCED
    ):
        failures.append("forecast_event_stage_not_announced")
    if (
        _REALIZED_ATTRIBUTION.search(structure.claim_summary)
        and not summary_forecast
        and _FUTURE_MODAL.search(structure.contractible_version)
    ):
        failures.append("realized_claim_changed_to_forecast")
    return failures


def _legacy_normalized_claim_gate(
    input_text: str,
    proposal: ExtractionProposal,
    *,
    legacy_v1_ambiguity_compatibility: bool = False,
) -> Loop1V2Result:
    if insider_screen(input_text):
        return Loop1V2Result(
            verdict=GateVerdict.REFUSED_INSIDER,
            reasons=["input_matched_nonpublic_information_screen"],
        )
    try:
        structure = ExtractedStructure.model_validate(
            proposal.structure.model_dump(mode="json")
        )
    except ValidationError as error:
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=[f"structure_revalidation_failed:{error.error_count()}"],
        )

    blocking, siblings, unclassified = _partition_ambiguities(structure.ambiguities)
    visible_text = " ".join(
        filter(
            None,
            [
                structure.claim_summary,
                structure.contractible_version,
                proposal.clarifying_question,
                *blocking,
                *siblings,
                *unclassified,
            ],
        )
    )
    violations = vocabulary_violations(visible_text)
    common = {
        "structure": structure,
        "blocking_ambiguities": blocking,
        "sibling_claims": siblings,
        "legacy_unclassified_ambiguities": unclassified,
    }
    if violations:
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=[f"restricted_vocabulary:{','.join(violations)}"],
            **common,
        )
    if unclassified and not legacy_v1_ambiguity_compatibility:
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=["unclassified_ambiguity_entries"],
            **common,
        )

    clarification_reasons = []
    if proposal.clarifying_question is not None:
        clarification_reasons.append("unresolved_clarification_question")
    if structure.stance is Stance.UNCLEAR:
        clarification_reasons.append("stance_unclear")
    if blocking:
        clarification_reasons.append("blocking_ambiguities_present")
    if clarification_reasons:
        question = proposal.clarifying_question or _FALLBACK_QUESTION.format(
            summary=structure.claim_summary
        )
        return Loop1V2Result(
            verdict=GateVerdict.NEEDS_CLARIFICATION,
            clarifying_question=question,
            reasons=clarification_reasons,
            **common,
        )

    failures = _grounding_failures(input_text, structure)
    if failures:
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=failures,
            **common,
        )
    return Loop1V2Result(verdict=GateVerdict.PASS, **common)


def _ready_grounding_failures(
    selected_source_context: str,
    structure: ExtractedStructure,
) -> list[str]:
    """Check high-confidence semantics against the selected source evidence.

    The model quote is first bound to one exact source occurrence and expanded
    to its containing sentence. This avoids treating an unrelated sibling
    sentence as the primary claim while preventing the model from omitting an
    adjacent trigger from both the quote and generated fields.
    """

    failures: list[str] = []
    summary = structure.claim_summary
    contract = structure.contractible_version
    normalized = f"{summary} {contract}"
    context_years = set(_YEAR.findall(selected_source_context))
    normalized_years = set(_YEAR.findall(normalized))
    if (
        str(structure.horizon.window_end.year) not in context_years
        or not normalized_years.issubset(context_years)
    ):
        failures.append("horizon_year_not_grounded_in_selected_source")

    if _LOWER_BOUND.search(selected_source_context):
        summary_has_lower = bool(_LOWER_BOUND.search(summary))
        contract_has_lower = bool(_LOWER_BOUND.search(contract))
        if not summary_has_lower:
            failures.append("lower_bound_missing_from_claim_summary")
        if not contract_has_lower:
            failures.append("lower_bound_missing_from_contractible_version")
        without_lower_bounds = _LOWER_BOUND.sub("", normalized)
        if _UPPER_BOUND.search(without_lower_bounds):
            failures.append("lower_bound_changed_to_upper_bound")

    if _FORECAST_ATTRIBUTION.search(selected_source_context):
        if not _FORECAST_ATTRIBUTION.search(summary):
            failures.append("forecast_attribution_missing_from_claim_summary")
        if not _FORECAST_ATTRIBUTION.search(contract):
            failures.append("forecast_attribution_missing_from_contractible_version")
        if structure.event_stage is not EventStage.ANNOUNCED:
            failures.append("forecast_event_stage_not_announced")

    source_is_realized = bool(
        _REALIZED_ATTRIBUTION.search(selected_source_context)
        and not _FUTURE_MODAL.search(selected_source_context)
    )
    if source_is_realized:
        if structure.event_stage is EventStage.ANNOUNCED:
            failures.append("realized_event_changed_to_announcement")
        if _FUTURE_MODAL.search(normalized):
            failures.append("realized_claim_changed_to_forecast")
    return failures


def _selected_source_context(input_text: str, quote: str) -> tuple[str | None, str | None]:
    """Resolve one quote occurrence to its complete containing sentence span."""

    starts: list[int] = []
    cursor = 0
    while True:
        start = input_text.find(quote, cursor)
        if start < 0:
            break
        starts.append(start)
        if len(starts) > 1:
            return None, "selected_source_quote_binding_ambiguous"
        cursor = start + 1
    if not starts:
        return None, "selected_source_quote_not_exact"

    quote_start = starts[0]
    quote_end = quote_start + len(quote)
    context_start = 0
    for boundary in _SENTENCE_BREAK.finditer(input_text, 0, quote_start):
        context_start = boundary.end()
    context_end = len(input_text)
    for boundary in _SENTENCE_BREAK.finditer(
        input_text, max(quote_start, quote_end - 1)
    ):
        if boundary.end() >= quote_end:
            context_end = boundary.end()
            break
    return input_text[context_start:context_end].strip(), None


def _ready_normalized_claim_gate(
    input_text: str,
    proposal: ExtractionProposalV2,
) -> Loop1V2Result:
    if insider_screen(input_text):
        return Loop1V2Result(
            verdict=GateVerdict.REFUSED_INSIDER,
            reasons=["input_matched_nonpublic_information_screen"],
        )

    if proposal.mode is ProposalModeV2.CLARIFICATION:
        clarification = proposal.clarification
        assert clarification is not None
        visible_text = " ".join(
            [
                clarification.question,
                *clarification.blocking_ambiguities,
                *clarification.sibling_claims,
            ]
        )
        violations = vocabulary_violations(visible_text)
        if violations:
            return Loop1V2Result(
                verdict=GateVerdict.REJECTED_INVALID,
                reasons=[f"restricted_vocabulary:{','.join(violations)}"],
            )
        return Loop1V2Result(
            verdict=GateVerdict.NEEDS_CLARIFICATION,
            clarifying_question=clarification.question,
            blocking_ambiguities=list(clarification.blocking_ambiguities),
            sibling_claims=list(clarification.sibling_claims),
            reasons=["blocking_ambiguities_present"],
        )

    candidate = proposal.candidate
    assert candidate is not None
    quote = candidate.selected_source_quote
    structure = candidate.structure
    if quote != quote.strip():
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            structure=structure,
            reasons=["selected_source_quote_not_exact"],
        )
    source_context, quote_failure = _selected_source_context(input_text, quote)
    if quote_failure is not None:
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            structure=structure,
            reasons=[quote_failure],
        )
    assert source_context is not None

    sibling_claims = list(structure.ambiguities)
    if any(
        not value.strip() or _AMBIGUITY_PREFIX.search(value)
        for value in sibling_claims
    ):
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            structure=structure,
            reasons=["candidate_ambiguities_not_plain_sibling_claims"],
        )
    visible_text = " ".join(
        [structure.claim_summary, structure.contractible_version, *sibling_claims]
    )
    violations = vocabulary_violations(visible_text)
    common = {"structure": structure, "sibling_claims": sibling_claims}
    if violations:
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=[f"restricted_vocabulary:{','.join(violations)}"],
            **common,
        )
    if structure.stance is Stance.UNCLEAR:
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=["candidate_stance_unclear_requires_clarification"],
            **common,
        )
    failures = _ready_grounding_failures(source_context, structure)
    if failures:
        return Loop1V2Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=failures,
            **common,
        )
    return Loop1V2Result(verdict=GateVerdict.PASS, **common)


def normalized_claim_gate(
    input_text: str,
    proposal: ExtractionProposal | ExtractionProposalV2,
    *,
    legacy_v1_ambiguity_compatibility: bool = False,
) -> Loop1V2Result:
    """Apply v2 semantics while preserving the consumed replay contract.

    New executions must use ``ExtractionProposalV2``.  The old v1-shaped
    envelope remains accepted here only so the sealed retrospective replay can
    be reproduced; ``ExtractionService`` rejects that mismatch for live v2.
    """

    if isinstance(proposal, ExtractionProposalV2):
        if legacy_v1_ambiguity_compatibility:
            raise ValueError("legacy ambiguity compatibility is v1-envelope only")
        return _ready_normalized_claim_gate(input_text, proposal)
    return _legacy_normalized_claim_gate(
        input_text,
        proposal,
        legacy_v1_ambiguity_compatibility=legacy_v1_ambiguity_compatibility,
    )
