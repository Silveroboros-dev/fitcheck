"""Fit advisory proposer adapters — Loop 3's model layer.

Agents propose; deterministic systems verify. The advisory NEVER owns
the class: its per-condition verdicts pass through the same cap table
as the deterministic checks (el.fitgate.gate) and can only demote below
the deterministic ceiling. Its suggested_class is telemetry.

Single call per (claim, market) pair — NO few-shot precedents, either
shape (H-P1 killed twice on the MFTA testbed; blueprint §10). The
rubric is the LIGHT epistemic engine (Appendix A distillation):
condition checks with verbatim quotes from BOTH sides, bridge
assumptions, a falsifier, numeric confidence, stop rule. Citation spans
are the anti-mad-libs mechanism — fields that must quote evidence
cannot be filled generically. The located deficit this targets:
condition verification (models transfer rule conclusions without
checking the rules' stated conditions).

GeminiFitProposer is the deployed default until 2026-08-17 (hackathon
rule); the gate is model-agnostic.
"""

import os
import uuid
from typing import Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field

from el.domain.enums import FitClass
from el.domain.structures import ExtractedStructure, MarketStructure

FIT_ADVISORY_POLICY_VERSION = "fit-advisory-v1"

# The named conditions the advisory must verify — the SAME vocabulary
# the gate's cap table speaks (el.fitgate.gate.ADVISORY_CAP_TABLE).
AdvisoryCondition = Literal[
    "same_event_stage",
    "same_metric",
    "horizon_covers_claim",
    "subject_is_claim_subject",
    "resolution_observes_truth_conditions",
]

# An advisory must verify EXACTLY these five — no more, no fewer. A
# missing condition is missing evidence, not "no failure"; the merge gate
# rejects an incomplete advisory (which falls back to the deterministic
# verdict). Derived from the Literal so the two never drift.
REQUIRED_ADVISORY_CONDITIONS: frozenset[str] = frozenset(
    get_args(AdvisoryCondition)
)


class ConditionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: AdvisoryCondition
    status: Literal["pass", "fail"]
    claim_evidence: str = Field(min_length=1, max_length=300)
    market_evidence: str = Field(min_length=1, max_length=300)


class FitAdvisory(BaseModel):
    """Judgment fields only; market_id is an echo the gate verifies."""

    model_config = ConfigDict(extra="forbid")

    market_id: str
    condition_verdicts: list[ConditionVerdict] = Field(
        min_length=1, max_length=5
    )
    bridge_assumptions: list[str] = Field(max_length=6)
    falsifier: str = Field(min_length=1, max_length=400)
    suggested_class: FitClass
    what_it_captures: str = Field(min_length=1, max_length=600)
    what_it_misses: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


class FitAdvisoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    advisory: FitAdvisory
    model_adapter: str
    model_run_id: str


class FitAdvisoryProposer(Protocol):
    def propose_fit(
        self,
        *,
        market_id: str,
        snapshot_id: str,
        claim_structure: ExtractedStructure,
        market_structure: MarketStructure,
        input_text: str,
        contract_terms_text: str,
        resolution_rules_text: str,
    ) -> FitAdvisoryResult: ...


class FixtureFitProposer:
    """Deterministic advisory fixtures keyed by market_id (eval truth
    never depends on a live model)."""

    def __init__(self, fixtures: dict[str, FitAdvisory]):
        self._fixtures = fixtures
        self.calls = 0

    def propose_fit(
        self,
        *,
        market_id: str,
        snapshot_id: str,
        claim_structure: ExtractedStructure,
        market_structure: MarketStructure,
        input_text: str,
        contract_terms_text: str,
        resolution_rules_text: str,
    ) -> FitAdvisoryResult:
        self.calls += 1
        return FitAdvisoryResult(
            advisory=self._fixtures[market_id],
            model_adapter="fixture",
            model_run_id=f"fixture-{self.calls}",
        )


_FIT_ADVISORY_PROMPT = """\
You audit whether ONE prediction market can express ONE claim. Work
only from the texts and structures given; never invent conditions that
are not written.

1. Evaluate ALL FIVE named conditions — same_event_stage, same_metric,
   horizon_covers_claim, subject_is_claim_subject,
   resolution_observes_truth_conditions. Output pass or fail for EVERY
   one; never omit a condition (a missing condition is a defective audit
   and is discarded). For each, give evidence that is an EXACT substring
   of the claim text AND an EXACT substring of the market rules —
   verbatim quotes, not paraphrases (paraphrased evidence is rejected).
   Never conclude from a rule without checking its stated conditions
   against the quoted evidence.
2. List the bridge assumptions: "for this market to express this
   claim, you assume X" — each named, checkable, cited. At most 6.
3. State the falsifier: the cheapest observation that would prove this
   fit wrong.
4. Give numeric confidence between 0 and 1. No hedge words anywhere.
5. Suggest a class: direct = distinguishing test of the claim;
   indirect = strong-evidence test; weak_proxy = confounded test (can
   pass while the claim is false); no_clean_expression = not a test of
   this claim at all.
6. Echo the market id exactly: {market_id}

Evaluate every condition even after you find a failure — do not stop
early. This is analysis, not advice; never use trading vocabulary.

Claim text:
---
{input_text}
---
Claim structure (extracted):
---
{claim_structure}
---
Market title:
---
{contract_terms_text}
---
Market resolution rules:
---
{resolution_rules_text}
---
Market structure (extracted):
---
{market_structure}
---
"""


class GeminiFitProposer:
    """Gemini structured-output advisory (lazy import; key via env)."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    def propose_fit(
        self,
        *,
        market_id: str,
        snapshot_id: str,
        claim_structure: ExtractedStructure,
        market_structure: MarketStructure,
        input_text: str,
        contract_terms_text: str,
        resolution_rules_text: str,
    ) -> FitAdvisoryResult:
        from google import genai  # lazy: tests never import the SDK

        client = genai.Client()  # GEMINI_API_KEY from env
        run_id = str(uuid.uuid4())
        response = client.models.generate_content(
            model=self.model,
            contents=_FIT_ADVISORY_PROMPT.format(
                market_id=market_id,
                input_text=input_text,
                claim_structure=claim_structure.model_dump_json(indent=2),
                contract_terms_text=contract_terms_text,
                resolution_rules_text=resolution_rules_text,
                market_structure=market_structure.model_dump_json(indent=2),
            ),
            config={
                "response_mime_type": "application/json",
                "response_schema": FitAdvisory,
                "temperature": 0.0,
            },
        )
        advisory = FitAdvisory.model_validate_json(response.text)
        return FitAdvisoryResult(
            advisory=advisory,
            model_adapter=f"gemini:{self.model}",
            model_run_id=run_id,
        )
