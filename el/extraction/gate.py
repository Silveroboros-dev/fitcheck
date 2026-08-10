"""Loop 1 — the normalized-claim gate.

Deterministic, failable, machine-checked (blueprint §3 gate rules). The
proposer suggests; this gate decides. Verdict semantics:

- PASS: structure is valid, unambiguous enough, vocabulary-clean.
- NEEDS_CLARIFICATION: valid but ambiguous — exactly one clarifying
  question goes back to the user; nothing persists yet.
- REFUSED_INSIDER: input seeks exposure from nonpublic information
  (A7 / compliance boundary); nothing persists, and the service refuses
  BEFORE any model call.
- REJECTED_INVALID: proposer output failed re-validation or used
  restricted vocabulary; lands in review as an eval candidate, never in
  front of the user.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from el.domain.enums import Stance
from el.domain.structures import ExtractedStructure
from el.domain.vocabulary import vocabulary_violations
from el.models.adapter import ExtractionProposal

GATE_POLICY_VERSION = "loop1-v1"

# More ambiguities than this without resolution => clarify first.
AMBIGUITY_THRESHOLD = 2

# v1 heuristic seed, governed via Loop 4 (additions need promotion).
# Deliberately conservative: false refusals are the safe direction.
_INSIDER_PATTERNS = re.compile(
    r"(insider (info|information|knowledge)|non-?public information"
    r"|\bmnpi\b|under nda|internal memo|leaked internal"
    r"|confidential (data|figures|numbers|deck|document)"
    r"|my (employer|company|client)'s (private|internal|confidential))",
    re.IGNORECASE,
)

INSIDER_REFUSAL_COPY = (
    "FitCheck can't help analyze market expression for claims based on "
    "confidential or nonpublic information. If the underlying claim is "
    "public, rephrase it using public sources."
)

_FALLBACK_CLARIFYING_QUESTION = (
    'Which specific, checkable outcome do you mean by: "{summary}"?'
)


class GateVerdict(StrEnum):
    PASS = "pass"
    NEEDS_CLARIFICATION = "needs_clarification"
    REFUSED_INSIDER = "refused_insider"
    REJECTED_INVALID = "rejected_invalid"


class Loop1Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: GateVerdict
    structure: ExtractedStructure | None = None
    clarifying_question: str | None = None
    reasons: list[str] = []
    gate_policy_version: str = GATE_POLICY_VERSION


def insider_screen(input_text: str) -> bool:
    """True if the input trips the nonpublic-information screen."""
    return bool(_INSIDER_PATTERNS.search(input_text))


def normalized_claim_gate(
    input_text: str, proposal: ExtractionProposal
) -> Loop1Result:
    # 1. Compliance screen (defense in depth — the service screens before
    #    the proposer ever runs; the gate re-checks regardless).
    if insider_screen(input_text):
        return Loop1Result(
            verdict=GateVerdict.REFUSED_INSIDER,
            reasons=["input matched nonpublic-information screen"],
        )

    # 2. Re-validate the structure. The gate never trusts the adapter,
    #    even though the adapter already validated (defense in depth).
    try:
        structure = ExtractedStructure.model_validate(
            proposal.structure.model_dump(mode="json")
        )
    except ValidationError as e:
        return Loop1Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=[f"structure failed re-validation: {e.error_count()} errors"],
        )

    # 3. Vocabulary gate on everything user-visible (A7).
    visible_text = " ".join(
        filter(
            None,
            [
                structure.claim_summary,
                structure.contractible_version,
                proposal.clarifying_question,
                *structure.ambiguities,
            ],
        )
    )
    violations = vocabulary_violations(visible_text)
    if violations:
        return Loop1Result(
            verdict=GateVerdict.REJECTED_INVALID,
            reasons=[f"restricted vocabulary: {', '.join(violations)}"],
        )

    # 4. Ambiguity rule: below threshold or exactly one question.
    ambiguous = (
        structure.stance is Stance.UNCLEAR
        or len(structure.ambiguities) > AMBIGUITY_THRESHOLD
    )
    if ambiguous:
        question = proposal.clarifying_question or (
            _FALLBACK_CLARIFYING_QUESTION.format(
                summary=structure.claim_summary
            )
        )
        return Loop1Result(
            verdict=GateVerdict.NEEDS_CLARIFICATION,
            structure=structure,
            clarifying_question=question,
            reasons=[
                "stance unclear"
                if structure.stance is Stance.UNCLEAR
                else f"{len(structure.ambiguities)} ambiguities "
                f"(threshold {AMBIGUITY_THRESHOLD})"
            ],
        )

    return Loop1Result(verdict=GateVerdict.PASS, structure=structure)
