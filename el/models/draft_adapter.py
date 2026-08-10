"""Draft-contract proposer adapters — Loop 3's draft-generation model layer.

Same discipline as the other proposer adapters (el.models.market_adapter,
el.models.fit_adapter): agents propose; deterministic systems verify. The
adapter produces a ProposedDraft; the draft gate (el.draftcontract.gate)
owns every decision about it.

The draft IS the productized "cheapest test to resolve the claim"
(Popperian reframe, blueprint Appendix A): when no existing market is a
clean expression, the draft is the contract that WOULD express it. The
model proposes JUDGMENT fields only; identity (thesis_analysis_id) is the
service's, never the model's. NO few-shot precedents (H-P1 killed twice
on the MFTA testbed; blueprint §10) — the rubric is the LIGHT epistemic
engine: a falsifiable resolution rule, a named observer, an echoed
deadline and subject. The echo fields are the anti-mad-libs mechanism:
the gate checks resolution_deadline against the claim window and
subject_entity against the claim's entities, so a draft cannot drift the
deadline (the eval_002 launch->announce failure) or be about nobody.

GeminiDraftProposer is the deployed default until 2026-08-17 (hackathon
rule); the gate is model-agnostic.
"""

import os
import uuid
from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from el.domain.enums import EventStage, ResolutionSourceClass
from el.domain.structures import ExtractedStructure

DRAFT_PROPOSER_POLICY_VERSION = "draft-proposer-v1"


class ProposedDraft(BaseModel):
    """Model-proposed draft fields — judgment only, no identity.

    resolution_deadline and subject_entity are echo fields: the gate
    verifies them against the claim, so they cannot be filled
    generically (the citation-span / anti-mad-libs discipline).
    """

    model_config = ConfigDict(extra="forbid")

    proposed_title: str = Field(min_length=1, max_length=512)
    proposed_resolution_logic: str = Field(min_length=1)
    resolution_source: str = Field(min_length=1, max_length=256)
    resolution_source_class: ResolutionSourceClass
    resolution_deadline: date
    subject_entity: str = Field(min_length=1)
    # The stage the draft RESOLVES on — an echo the gate checks against the
    # claim's event_stage (D7): a draft must resolve on the claim's stage,
    # never a weaker proxy (e.g. announce for a launch claim).
    event_stage: EventStage
    category: str | None = Field(default=None, max_length=64)
    time_horizon: str | None = Field(default=None, max_length=64)


class DraftProposerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: ProposedDraft
    model_adapter: str
    model_run_id: str


class DraftContractProposer(Protocol):
    def propose_draft(
        self,
        *,
        thesis_analysis_id: uuid.UUID,
        claim_structure: ExtractedStructure,
        rejection_reasons: list[str],
    ) -> DraftProposerResult: ...


class FixtureDraftProposer:
    """Deterministic proposer over golden drafts, keyed by claim summary
    (eval truth never depends on a live model)."""

    def __init__(self, by_summary: dict[str, ProposedDraft]):
        self._by_summary = by_summary
        self.calls = 0

    def propose_draft(
        self,
        *,
        thesis_analysis_id: uuid.UUID,
        claim_structure: ExtractedStructure,
        rejection_reasons: list[str],
    ) -> DraftProposerResult:
        self.calls += 1
        return DraftProposerResult(
            draft=self._by_summary[claim_structure.claim_summary],
            model_adapter="fixture",
            model_run_id=f"fixture-{self.calls}",
        )


def _format_rejections(reasons: list[str]) -> str:
    if not reasons:
        return "(no candidate markets were evaluated — design from the claim alone)"
    return "\n".join(f"- {reason}" for reason in reasons)


_DRAFT_GEN_PROMPT = """\
You design the single cheapest prediction market that would cleanly
resolve ONE claim — a distinguishing test of it. No existing market
expresses this claim; your draft is the contract that WOULD. Work only
from the claim below; never invent facts about the world.

The existing candidate markets were refused for these defects — your
draft must fix exactly these and introduce none:
{rejection_reasons}

Produce:
- proposed_title: ONE yes/no question that names the claim's subject, the
  exact resolving condition, and the deadline.
- proposed_resolution_logic: the single observation that settles YES,
  written so two readers would agree on the outcome. State the
  observation, not a narrative.
- resolution_source: the concrete entity that attests the outcome (an
  office, regulator, filing, leaderboard, named report). A test with no
  observer is not a test.
- resolution_source_class: official, leaderboard, filing, or press. Do
  NOT return none — if the claim itself names no observer, propose the
  cheapest credible one (that is the whole point of the draft).
- resolution_deadline: the claim's resolution deadline — its horizon
  window end — as an ISO date. Do not drift it to a settlement date.
- subject_entity: the claim's subject, named as a reader would name it.
- event_stage: the stage the draft RESOLVES on (announced, launched,
  shipped, adopted, measured, resolved). It MUST be the claim's own event
  stage — never a weaker proxy. Resolving on 'announced' for a 'launched'
  claim is a different, easier event and is rejected.
- category, time_horizon: short labels.

Falsifiability over assertion: the resolution logic must state the
observation that would settle the question. No hedge words anywhere.
This is analysis, not advice; never use trading vocabulary.

Claim:
---
{claim_summary}
---
Claim structure (extracted):
---
{claim_structure}
---
"""


class GeminiDraftProposer:
    """Gemini structured-output draft proposer (lazy import; key via env)."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    def propose_draft(
        self,
        *,
        thesis_analysis_id: uuid.UUID,
        claim_structure: ExtractedStructure,
        rejection_reasons: list[str],
    ) -> DraftProposerResult:
        from google import genai  # lazy: tests never import the SDK

        client = genai.Client()  # GEMINI_API_KEY from env
        run_id = str(uuid.uuid4())
        response = client.models.generate_content(
            model=self.model,
            contents=_DRAFT_GEN_PROMPT.format(
                claim_summary=claim_structure.claim_summary,
                claim_structure=claim_structure.model_dump_json(indent=2),
                rejection_reasons=_format_rejections(rejection_reasons),
            ),
            config={
                "response_mime_type": "application/json",
                "response_schema": ProposedDraft,
                "temperature": 0.0,
            },
        )
        draft = ProposedDraft.model_validate_json(response.text)
        return DraftProposerResult(
            draft=draft,
            model_adapter=f"gemini:{self.model}",
            model_run_id=run_id,
        )
