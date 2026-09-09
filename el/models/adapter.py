"""Proposer adapters — Loop 1's model layer.

Agents propose; deterministic systems verify. Adapters produce an
ExtractionProposal; the Loop 1 gate (el.extraction.gate) owns every
decision about it. No adapter result reaches a user or the ledger
without passing the gate.

GeminiProposer is the default in the deployed app until 2026-08-17
(hackathon rule — docs/hackathon-constraints.md). Other adapters may run
alongside; the gate is model-agnostic.
"""

import os
import uuid
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from el.domain.structures import ExtractedStructure


class ExtractionProposal(BaseModel):
    """Adapter envelope: the frozen structure plus proposer-only fields.

    clarifying_question lives here, NOT in ExtractedStructure — the
    frozen schema stays frozen; the envelope is adapter-layer.
    """

    model_config = ConfigDict(extra="forbid")

    structure: ExtractedStructure
    clarifying_question: str | None = Field(default=None, max_length=300)


class ProposerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: ExtractionProposal
    model_adapter: str
    model_run_id: str


class ProposerAdapter(Protocol):
    def propose_extraction(self, input_text: str) -> ProposerResult: ...


class FixtureProposer:
    """Deterministic proposer for tests and frozen-snapshot evals.

    Eval truth never depends on a live model (invariant: frozen snapshots
    are eval truth; live retrieval is candidate evidence).
    """

    def __init__(self, fixtures: dict[str, ExtractionProposal]):
        self._fixtures = fixtures
        self.calls = 0

    def propose_extraction(self, input_text: str) -> ProposerResult:
        self.calls += 1
        proposal = self._fixtures[input_text]
        return ProposerResult(
            proposal=proposal,
            model_adapter="fixture",
            model_run_id=f"fixture-{self.calls}",
        )

EXTRACTION_PROMPT_POLICY_VERSION = "loop1-extraction-prompt-v1"


_EXTRACTION_PROMPT = """\
You normalize messy claims about future events into a precise, testable
structure for an expression-risk audit.

Rules:
- Extract the single most contractible claim. If the text bundles several
  claims, pick the most checkable one and note the rest in `ambiguities`.
- `contractible_version` must name a checkable outcome, a deadline, and a
  resolution source class.
- Dates are ISO (YYYY-MM-DD). If the text implies a year only, use its
  last day and precision "year".
- `stance` is one of: yes, no, increase, decrease, outperform,
  underperform, unclear.
- A7 (binding): NEVER use the bare words "buy" or "sell" in ANY generated
  field (claim_summary, contractible_version, metric.what, ambiguities). For
  an acquisition or divestiture write "acquire"/"divest" (or "purchase"/
  "dispose") even when the underlying event is a sale or purchase — the
  user's raw wording is preserved separately; our output must stay
  advice-clean. This is analysis, not a recommendation.
- If the claim is genuinely ambiguous, fill `ambiguities` and propose
  exactly ONE `clarifying_question`. Otherwise leave it null.
- This is analysis, not advice. No recommendations of any kind.

Text to normalize:
---
{input_text}
---
"""


class GeminiProposer:
    """Gemini structured-output proposer (lazy import; key via env)."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    def propose_extraction(self, input_text: str) -> ProposerResult:
        from google import genai  # lazy: tests never import the SDK

        client = genai.Client()  # GEMINI_API_KEY from env
        run_id = str(uuid.uuid4())
        response = client.models.generate_content(
            model=self.model,
            contents=_EXTRACTION_PROMPT.format(input_text=input_text),
            config={
                "response_mime_type": "application/json",
                "response_schema": ExtractionProposal,
                "temperature": 0.0,
            },
        )
        proposal = ExtractionProposal.model_validate_json(response.text)
        return ProposerResult(
            proposal=proposal,
            model_adapter=f"gemini:{self.model}",
            model_run_id=run_id,
        )
