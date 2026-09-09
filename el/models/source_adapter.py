"""Source interpretation adapters for Product Discovery v3.1.

This layer identifies distinct source-grounded thesis candidates.  It never
chooses which candidate matters to the user and never normalizes one into
accepted thesis truth.
"""

from __future__ import annotations

import os
import uuid
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)


SOURCE_INTERPRETATION_PROMPT_POLICY_VERSION_V1 = "source-thesis-candidates-v1"

SOURCE_INTERPRETATION_PROMPT_V1 = """\
Read the source text and identify one to three distinct, decision-relevant
claims about future events that a person could later refine and test.

Rules:
- Return candidates in source order. Do not rank them, choose a primary claim,
  or infer which claim matters most to the user.
- Each `selected_source_quote` must be an exact, non-empty substring copied
  from the source and must contain the complete claim represented by that
  candidate.
- `claim_summary` is a concise source-faithful description. Preserve causal
  direction, modality, event stage, thresholds, temporal operators, and
  attribution. Do not invent a horizon or resolution criterion.
- Keep materially distinct claims separate. Do not split supporting details
  into candidates when they do not form a testable claim.
- Never use the bare words "buy" or "sell" in generated summaries. Use
  acquire/divest or purchase/dispose for transaction events. Do not provide
  recommendations.
- If the text cannot safely or meaningfully yield a future-facing thesis,
  return refusal mode with one or more short reasons.

Source text:
---
{input_text}
---
"""

# Preserve the first live prompt as evidence. This successor makes candidate
# coverage explicit after a multi-claim source yielded only one candidate.
# Completeness remains an evaluated model property, not a deterministic truth.
SOURCE_INTERPRETATION_PROMPT_POLICY_VERSION = "source-thesis-candidates-v1.1"
SOURCE_INTERPRETATION_PROMPT = """\
Candidate coverage protocol:
- First scan the complete source for independently testable future claims.
- If the source contains claims about different subjects, outcomes, or causal
  relationships, return each eligible claim separately, up to three.
- Do not omit or merge an eligible claim merely because another claim is more
  concrete, prominent, or easier to normalize.

""" + SOURCE_INTERPRETATION_PROMPT_V1


class SourceInterpretationMode(StrEnum):
    CANDIDATES = "candidates"
    REFUSAL = "refusal"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceThesisCandidateProposal(_Frozen):
    selected_source_quote: str = Field(min_length=1, max_length=8_000)
    claim_summary: str = Field(min_length=1, max_length=1_000)


class SourceInterpretationProposal(_Frozen):
    mode: SourceInterpretationMode
    candidate_items: list[SourceThesisCandidateProposal] = Field(max_length=3)
    refusal_reasons: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def _shape(self) -> "SourceInterpretationProposal":
        if self.mode is SourceInterpretationMode.CANDIDATES:
            if not 1 <= len(self.candidate_items) <= 3 or self.refusal_reasons:
                raise ValueError(
                    "candidate mode requires one to three candidates and no refusal"
                )
        elif self.candidate_items or not self.refusal_reasons:
            raise ValueError("refusal mode requires reasons and no candidates")
        if any(not value.strip() for value in self.refusal_reasons):
            raise ValueError("refusal reasons must be non-empty")
        return self


class SourceInterpreterResult(_Frozen):
    proposal: SourceInterpretationProposal
    model_adapter: str
    model_run_id: str


class SourceProviderOutcomeUncertain(RuntimeError):
    """The adapter cannot prove whether the external operation completed."""


class SourceModelOutputInvalid(ValueError):
    """A provider response was received but failed the declared schema."""


class SourceInterpreterAdapter(Protocol):
    prompt_policy_version: str
    adapter_kind: str
    model_id: str
    has_external_effect: bool

    def propose_candidates(self, input_text: str) -> SourceInterpreterResult: ...


class FixtureSourceInterpreter:
    prompt_policy_version = "fixture-projection-of-source-thesis-candidates-v1"
    adapter_kind = "fixture"
    model_id = "fixture-source-v1"
    has_external_effect = False

    def __init__(self, fixtures: dict[str, SourceInterpretationProposal]):
        self._fixtures = fixtures
        self.calls = 0

    def propose_candidates(self, input_text: str) -> SourceInterpreterResult:
        self.calls += 1
        return SourceInterpreterResult(
            proposal=self._fixtures[input_text],
            model_adapter="fixture-source-v1",
            model_run_id=f"fixture-source-v1-{self.calls}",
        )


class GeminiSourceInterpreter:
    prompt_policy_version: Literal["source-thesis-candidates-v1.1"] = (
        SOURCE_INTERPRETATION_PROMPT_POLICY_VERSION
    )
    adapter_kind = "gemini"
    has_external_effect = True

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    @property
    def model_id(self) -> str:
        return self.model

    def propose_candidates(self, input_text: str) -> SourceInterpreterResult:
        from google import genai  # lazy: offline tests never import the SDK

        client = genai.Client()
        run_id = str(uuid.uuid4())
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=SOURCE_INTERPRETATION_PROMPT.format(input_text=input_text),
                config={
                    "response_mime_type": "application/json",
                    "response_schema": SourceInterpretationProposal,
                    "temperature": 0.0,
                },
            )
        except Exception as error:
            raise SourceProviderOutcomeUncertain(
                "source provider operation outcome is uncertain"
            ) from error
        try:
            proposal = SourceInterpretationProposal.model_validate_json(
                response.text
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise SourceModelOutputInvalid(
                "source provider returned an invalid response schema"
            ) from error
        return SourceInterpreterResult(
            proposal=proposal,
            model_adapter=f"gemini:{self.model}",
            model_run_id=run_id,
        )


__all__ = [
    "FixtureSourceInterpreter",
    "GeminiSourceInterpreter",
    "SOURCE_INTERPRETATION_PROMPT",
    "SOURCE_INTERPRETATION_PROMPT_POLICY_VERSION",
    "SOURCE_INTERPRETATION_PROMPT_POLICY_VERSION_V1",
    "SOURCE_INTERPRETATION_PROMPT_V1",
    "SourceInterpretationMode",
    "SourceInterpretationProposal",
    "SourceModelOutputInvalid",
    "SourceProviderOutcomeUncertain",
    "SourceInterpreterAdapter",
    "SourceInterpreterResult",
    "SourceThesisCandidateProposal",
]
