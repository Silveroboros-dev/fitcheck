"""Loop 1 v2 proposer kept separate from the hash-bound v1 adapter module.

The first v2 prompt was used for the consumed retrospective replay and remains
available under its original constants.  The live-ready successor uses an
explicit candidate-or-clarification envelope so a missing horizon never forces
the model to invent a date merely to satisfy ``ExtractedStructure`` v1.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from el.domain.structures import ExtractedStructure
from el.models.adapter import GeminiProposer


EXTRACTION_PROMPT_POLICY_VERSION_V2 = "loop1-extraction-prompt-v2"

EXTRACTION_PROMPT_V2 = """\
You normalize messy claims about future events into one precise, testable
claim for an expression-risk audit.

Rules:
- Extract one primary claim: the claim that can be rendered most faithfully
  as a public, checkable contract. Do not change its modality, event stage,
  metric, direction, threshold, time boundary, or source attribution merely
  to make it easier to contract.
- `contractible_version` must preserve the selected claim. A reported
  forecast, target, expectation, or guidance is a claim about that public
  forecast; do not silently turn it into a claim that the forecasted outcome
  will actually occur. Likewise, do not turn a realized event into a forecast.
- Preserve temporal operators exactly. In particular, `not before` / `on or
  after` is a lower bound and must never become `by` / `on or before`.
- Never invent a year, date, or reference time. A stated year may be expanded
  to its last day with precision `year`. If a usable horizon cannot be grounded
  in the text, record a blocking ambiguity and ask one question.
- Every `ambiguities` entry must start with one of two tags:
  * `blocking:` for missing information that can change the primary claim's
    contract meaning;
  * `sibling:` for another bundled/background claim that was not selected.
  Sibling claims are preserved for disclosure but never trigger clarification
  by themselves.
- Emit exactly one `clarifying_question` only when a `blocking:` ambiguity
  remains. Otherwise leave it null. A non-null question blocks persistence.
- `stance` is one of: yes, no, increase, decrease, outperform,
  underperform, unclear.
- A7 (binding): NEVER use the bare words "buy" or "sell" in ANY generated
  field (claim_summary, contractible_version, metric.what, ambiguities). For
  an acquisition or divestiture write "acquire"/"divest" (or "purchase"/
  "dispose") even when the underlying event is a sale or purchase — the
  user's raw wording is preserved separately; our output must stay
  advice-clean. This is analysis, not a recommendation.
- This is analysis, not advice. No recommendations of any kind.

Text to normalize:
---
{input_text}
---
"""

# The consumed replay above is immutable evidence.  The live-ready prompt is a
# successor contract rather than a silent rewrite of those historical bytes.
EXTRACTION_PROMPT_POLICY_VERSION_V2_READY = "loop1-extraction-prompt-v2.1"

EXTRACTION_PROMPT_V2_READY = """\
You normalize messy claims about future events for an expression-risk audit.

Return exactly one of two modes:

1. `candidate`: use this only when one primary claim can be represented
   faithfully and has a source-grounded time horizon. Set `clarification` to
   null. The candidate must contain:
   - `selected_source_quote`: an exact, non-empty substring copied from the
     input that contains the complete selected claim;
   - `structure`: the complete normalized claim.
2. `clarification`: use this whenever missing information can change the
   primary claim's contract meaning. Set `candidate` to null. Include one
   question and at least one blocking ambiguity. Do not invent a date, year,
   stance, event stage, metric, threshold, or source in this mode.

Candidate rules:
- Select the primary claim that can be rendered most faithfully as a public,
  checkable contract. Do not change modality, event stage, metric, direction,
  threshold, temporal operator, horizon, or source attribution merely to make
  it easier to contract.
- Preserve a reported forecast, target, expectation, or guidance as a claim
  about that public statement. Do not turn it into a claim that the forecasted
  outcome occurred or will occur. Do not turn a realized event into a forecast.
- Preserve temporal operators in both `claim_summary` and
  `contractible_version`. In particular, `not before`, `no earlier than`, and
  `on or after` are lower bounds, never `by` or `on or before`.
- Never invent a year, date, or reference time. A stated year may be expanded
  to its last day with precision `year`.
- `structure.ambiguities` contains only unselected sibling claims, without
  `blocking:` or `sibling:` prefixes. A blocking ambiguity requires
  `clarification` mode instead.
- `stance` is one of: yes, no, increase, decrease, outperform,
  underperform, unclear. A genuinely unclear stance requires clarification.
- Never use the bare words "buy" or "sell" in any generated field. Use
  acquire/divest or purchase/dispose for transaction events. This is analysis,
  not advice, and must contain no recommendation.

Text to normalize:
---
{input_text}
---
"""

# The v2.1 prompt above has live product evidence and remains immutable. This
# successor disambiguates envelope/prompt versioning from the frozen structure
# schema after Gemini emitted ``structure.schema_version = 2`` in a live UI run.
EXTRACTION_PROMPT_POLICY_VERSION_V2_PRODUCT = "loop1-extraction-prompt-v2.3"
EXTRACTION_PROMPT_V2_PRODUCT = """\
Critical schema rule:
- `candidate.structure.schema_version` MUST be the integer `1`.
- The candidate/clarification envelope is version 2; that does not change the
  frozen `ExtractedStructure` schema version. Never emit 2 for schema_version.

""" + EXTRACTION_PROMPT_V2_READY

# Vertex managed batch has not proven required nullable object branches.  Keep
# the v2.1 prompt above immutable for its existing retrospective evidence and
# use this successor only with the provider-safe array envelope below.
EXTRACTION_PROMPT_POLICY_VERSION_V2_MANAGED_WIRE = (
    "loop1-extraction-prompt-v2.2"
)

EXTRACTION_PROMPT_V2_MANAGED_WIRE = """\
You normalize messy claims about future events for an expression-risk audit.

Return one JSON object with exactly these three required fields: `mode`,
`candidate_items`, and `clarification_items`.

1. For `candidate` mode, set `candidate_items` to an array containing exactly
   one candidate and set `clarification_items` to an empty array. The candidate
   must contain:
   - `selected_source_quote`: an exact, non-empty substring copied from the
     input that contains the complete selected claim;
   - `structure`: the complete normalized claim.
2. For `clarification` mode, set `candidate_items` to an empty array and set
   `clarification_items` to an array containing exactly one clarification.
   Include one question and at least one blocking ambiguity. Do not invent a
   date, year, stance, event stage, metric, threshold, or source in this mode.

Candidate rules:
- Select the primary claim that can be rendered most faithfully as a public,
  checkable contract. Do not change modality, event stage, metric, direction,
  threshold, temporal operator, horizon, or source attribution merely to make
  it easier to contract.
- Preserve a reported forecast, target, expectation, or guidance as a claim
  about that public statement. Do not turn it into a claim that the forecasted
  outcome occurred or will occur. Do not turn a realized event into a forecast.
- Preserve temporal operators in both `claim_summary` and
  `contractible_version`. In particular, `not before`, `no earlier than`, and
  `on or after` are lower bounds, never `by` or `on or before`.
- Never invent a year, date, or reference time. A stated year may be expanded
  to its last day with precision `year`.
- `structure.ambiguities` contains only unselected sibling claims, without
  `blocking:` or `sibling:` prefixes. A blocking ambiguity requires
  `clarification` mode instead.
- `stance` is one of: yes, no, increase, decrease, outperform,
  underperform, unclear. A genuinely unclear stance requires clarification.
- Never use the bare words "buy" or "sell" in any generated field. Use
  acquire/divest or purchase/dispose for transaction events. This is analysis,
  not advice, and must contain no recommendation.

Text to normalize:
---
{input_text}
---
"""


class ProposalModeV2(StrEnum):
    CANDIDATE = "candidate"
    CLARIFICATION = "clarification"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateProposalV2(_Frozen):
    structure: ExtractedStructure
    selected_source_quote: str = Field(min_length=1, max_length=8_000)


class ClarificationProposalV2(_Frozen):
    blocking_ambiguities: list[str] = Field(min_length=1)
    sibling_claims: list[str] = Field(default_factory=list)
    question: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def _nonempty_text(self) -> "ClarificationProposalV2":
        if any(not value.strip() for value in self.blocking_ambiguities):
            raise ValueError("blocking ambiguities must be non-empty")
        if any(not value.strip() for value in self.sibling_claims):
            raise ValueError("sibling claims must be non-empty")
        if len(self.blocking_ambiguities) > 12 or len(self.sibling_claims) > 12:
            raise ValueError("clarification lists exceed the v2 bound")
        if not self.question.strip():
            raise ValueError("clarification question must be non-empty")
        return self


# The historical docstring below contributes to the pinned semantic-schema
# digest.  Managed batch now treats this as the local semantic value and uses
# ``ExtractionProposalV2ProviderWire`` for transport; do not rewrite the text.
class ExtractionProposalV2(_Frozen):
    """Live-ready v2 wire contract with exactly one populated branch."""

    mode: ProposalModeV2
    candidate: CandidateProposalV2 | None
    clarification: ClarificationProposalV2 | None

    @model_validator(mode="after")
    def _exclusive_mode(self) -> "ExtractionProposalV2":
        candidate_mode = self.mode is ProposalModeV2.CANDIDATE
        if candidate_mode != (self.candidate is not None):
            raise ValueError("proposal mode and candidate branch differ")
        if candidate_mode == (self.clarification is not None):
            raise ValueError("exactly one proposal branch is required")
        return self


class ExtractionProposalV2ProviderWire(_Frozen):
    """Provider-safe wire shape converted into ``ExtractionProposalV2``.

    Vertex receives no nullable complex-object unions.  Both arrays are
    required by the schema; this deterministic validator, rather than the
    provider, owns the exact-one-branch invariant.
    """

    mode: ProposalModeV2
    candidate_items: list[CandidateProposalV2]
    clarification_items: list[ClarificationProposalV2]

    @model_validator(mode="after")
    def _exclusive_mode(self) -> "ExtractionProposalV2ProviderWire":
        if self.mode is ProposalModeV2.CANDIDATE:
            if len(self.candidate_items) != 1 or self.clarification_items:
                raise ValueError(
                    "candidate wire mode requires one candidate and no clarification"
                )
        elif self.candidate_items or len(self.clarification_items) != 1:
            raise ValueError(
                "clarification wire mode requires one clarification and no candidate"
            )
        return self

    def to_proposal(self) -> ExtractionProposalV2:
        if self.mode is ProposalModeV2.CANDIDATE:
            return ExtractionProposalV2(
                mode=self.mode,
                candidate=self.candidate_items[0],
                clarification=None,
            )
        return ExtractionProposalV2(
            mode=self.mode,
            candidate=None,
            clarification=self.clarification_items[0],
        )

    @classmethod
    def from_proposal(
        cls, proposal: ExtractionProposalV2
    ) -> "ExtractionProposalV2ProviderWire":
        semantic = ExtractionProposalV2.model_validate(
            proposal.model_dump(mode="json")
        )
        return cls(
            mode=semantic.mode,
            candidate_items=(
                [semantic.candidate] if semantic.candidate is not None else []
            ),
            clarification_items=(
                [semantic.clarification]
                if semantic.clarification is not None
                else []
            ),
        )


def parse_extraction_proposal_v2_provider_wire(value: object) -> ExtractionProposalV2:
    """Validate provider JSON and return the unchanged local semantic model."""

    return ExtractionProposalV2ProviderWire.model_validate(value).to_proposal()


class ProposerResultV2(_Frozen):
    proposal: ExtractionProposalV2
    model_adapter: str
    model_run_id: str


class ModelOutputValidationFailure(RuntimeError):
    """Sanitized invalid-provider-output evidence; never carries raw bytes."""

    def __init__(
        self,
        *,
        model_adapter: str,
        model_run_id: str,
        reasons: list[str],
    ):
        self.model_adapter = model_adapter
        self.model_run_id = model_run_id
        self.reasons = tuple(reasons)
        super().__init__("model output failed the declared response schema")


def _validation_reason_codes(error: ValidationError) -> list[str]:
    reasons: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "root"
        error_type = str(item.get("type", "validation_error"))
        reasons.append(f"model_output_schema_invalid:{location}:{error_type}")
    return reasons or ["model_output_schema_invalid:root:validation_error"]


class FixtureProposerV2:
    """Deterministic v2 proposer for offline tests and frozen fixtures."""

    def __init__(self, fixtures: dict[str, ExtractionProposalV2]):
        self._fixtures = fixtures
        self.calls = 0

    def propose_extraction(self, input_text: str) -> ProposerResultV2:
        self.calls += 1
        return ProposerResultV2(
            proposal=self._fixtures[input_text],
            model_adapter="fixture-v2",
            model_run_id=f"fixture-v2-{self.calls}",
        )


class GeminiProposerV2(GeminiProposer):
    """Gemini proposer using the product v2.3 prompt and v2 envelope."""

    prompt_policy_version: Literal["loop1-extraction-prompt-v2.3"] = (
        EXTRACTION_PROMPT_POLICY_VERSION_V2_PRODUCT
    )

    def propose_extraction(self, input_text: str) -> ProposerResultV2:
        from google import genai  # lazy: tests never import the SDK

        client = genai.Client()  # GEMINI_API_KEY from env
        run_id = str(uuid.uuid4())
        response = client.models.generate_content(
            model=self.model,
            contents=EXTRACTION_PROMPT_V2_PRODUCT.format(input_text=input_text),
            config={
                "response_mime_type": "application/json",
                "response_schema": ExtractionProposalV2,
                "temperature": 0.0,
            },
        )
        model_adapter = f"gemini:{self.model}"
        try:
            proposal = ExtractionProposalV2.model_validate_json(response.text)
        except ValidationError as error:
            raise ModelOutputValidationFailure(
                model_adapter=model_adapter,
                model_run_id=run_id,
                reasons=_validation_reason_codes(error),
            ) from error
        return ProposerResultV2(
            proposal=proposal,
            model_adapter=model_adapter,
            model_run_id=run_id,
        )


__all__ = [
    "CandidateProposalV2",
    "ClarificationProposalV2",
    "EXTRACTION_PROMPT_POLICY_VERSION_V2",
    "EXTRACTION_PROMPT_POLICY_VERSION_V2_READY",
    "EXTRACTION_PROMPT_POLICY_VERSION_V2_PRODUCT",
    "EXTRACTION_PROMPT_POLICY_VERSION_V2_MANAGED_WIRE",
    "EXTRACTION_PROMPT_V2",
    "EXTRACTION_PROMPT_V2_MANAGED_WIRE",
    "EXTRACTION_PROMPT_V2_READY",
    "EXTRACTION_PROMPT_V2_PRODUCT",
    "ExtractionProposalV2",
    "ExtractionProposalV2ProviderWire",
    "FixtureProposerV2",
    "GeminiProposerV2",
    "ModelOutputValidationFailure",
    "ProposalModeV2",
    "ProposerResultV2",
    "parse_extraction_proposal_v2_provider_wire",
]
