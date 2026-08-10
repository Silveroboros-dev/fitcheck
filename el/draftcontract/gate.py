"""Draft-contract gate — validates a generated draft (blueprint §13 step 5b).

Draft generation is a proposer + gate, the same discipline as market-side
extraction (el.marketstructure.gate): a model proposes the draft, this
gate validates it. The draft IS the productized "cheapest test to resolve
the claim" (Popperian reframe, blueprint Appendix A) — so the gate's job
is to refuse drafts that are not actually tests.

Verdicts:
- PASS: schema-valid, vocabulary-clean, deadline matches the claim
  window, subject echoed, resolution observable.
- REJECTED_INVALID: any check fails. A rejected draft never persists; the
  fit card stays valid WITHOUT a draft (graceful no-draft, service layer).

Six checks (D1-D6), each producing a logged outcome for provenance:
  D1 schema re-validate  — defense in depth (never trust the adapter).
  D2 vocabulary (A7)     — restricted terms in any text field -> reject.
  D3 deadline anti-drift — resolution_deadline == claim window end; the
                           eval_002 launch->announce drift, productized as
                           a guardable echo field.
  D4 subject echo        — the declared subject is a real claim subject
                           AND its token appears in title/logic (governed
                           tok_v1; the citation-span / anti-mad-libs check).
  D5 observability       — source class != none and a source is named; the
                           draft must UPGRADE the claim's resolvability,
                           never inherit its 'none'. A test with no
                           observer is not a test.
  D6 object+metric echo  — the claim's object-role entities AND its metric
                           tokens must appear in the draft; a shape-valid
                           draft that drifts WHAT is measured is not an
                           expression of the thesis.
  D7 event-stage match   — the draft must resolve on the claim's event
                           stage, never a weaker proxy (announce-for-launch
                           drift); the analog of the fit gate's S1.

The gate consumes the proposer's ProposedDraft (like el.fitgate.gate
consumes FitAdvisory). D6 (object/metric echo) and D7 (event-stage) are
token-presence / declared-field checks, NOT a full structural re-fit of the
draft against the claim. KNOWN LIMIT (external review 2026-06-13): D7
compares the proposer's DECLARED event_stage field, so a draft whose PROSE
says "announce" while its field says "launched" still passes — the same
announce-vs-launch drift D7 was meant to catch. A gate-PASS draft is
therefore a SHAPE-VALID CANDIDATE, never a verified clean expression (claim
discipline; see el.domain.contracts.DraftContractOut).

BOOKED (Loop 4 — required before external testers or any demo presenting
drafts as clean expressions): build a DraftMarketStructure from the draft
and run the deterministic fit gate over ExtractedStructure x
DraftMarketStructure (the structure-to-structure principle, bounded by v1
composite/causal limits). A lexical D8 prose-stage guard is NOT added unless
the structural re-fit is blocked; if ever added it is explicitly temporary
and must not justify stronger product copy. Rejection-fixing of EXISTING
markets stays a proposer INPUT, not a gate assertion in v1.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from el.domain.enums import ResolutionSourceClass
from el.domain.structures import ExtractedStructure
from el.domain.vocabulary import vocabulary_violations
from el.fitgate.checks import TOKEN_RULES_VERSION, tok_v1
from el.models.draft_adapter import ProposedDraft

GATE_POLICY_VERSION = "draftgen-v1"


class DraftGateVerdict(StrEnum):
    PASS = "pass"
    REJECTED_INVALID = "rejected_invalid"


class DraftCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    name: str
    status: Literal["pass", "fail"]
    detail: str


class DraftGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: DraftGateVerdict
    draft: ProposedDraft | None = None
    checks: list[DraftCheck]
    reasons: list[str] = []
    gate_policy_version: str = GATE_POLICY_VERSION
    token_rules_version: str = TOKEN_RULES_VERSION


def _claim_subjects(claim: ExtractedStructure) -> list[str]:
    """Subject-role entity names, falling back to all entities so the
    echo check never passes vacuously on a subjectless claim."""
    subjects = [e.name for e in claim.entities if e.role == "subject"]
    return subjects or [e.name for e in claim.entities]


def draft_contract_gate(
    *, claim: ExtractedStructure, proposed: ProposedDraft
) -> DraftGateResult:
    # D1 — re-validate (defense in depth; catches a model_construct bypass).
    try:
        validated = ProposedDraft.model_validate(proposed.model_dump(mode="json"))
    except ValidationError as error:
        return DraftGateResult(
            verdict=DraftGateVerdict.REJECTED_INVALID,
            checks=[
                DraftCheck(
                    check_id="D1",
                    name="schema_revalidate",
                    status="fail",
                    detail=f"{error.error_count()} schema errors on re-validation",
                )
            ],
            reasons=["draft failed schema re-validation"],
        )
    checks = [
        DraftCheck(
            check_id="D1",
            name="schema_revalidate",
            status="pass",
            detail="schema valid",
        )
    ]

    # D2 — A7 vocabulary over every text field.
    text = " ".join(
        filter(
            None,
            [
                validated.proposed_title,
                validated.proposed_resolution_logic,
                validated.resolution_source,
                validated.subject_entity,
                validated.category,
                validated.time_horizon,
            ],
        )
    )
    violations = vocabulary_violations(text)
    checks.append(
        DraftCheck(
            check_id="D2",
            name="vocabulary",
            status="fail" if violations else "pass",
            detail=(
                f"restricted vocabulary: {', '.join(violations)}"
                if violations
                else "vocabulary clean"
            ),
        )
    )

    # D3 — deadline anti-drift.
    deadline_ok = validated.resolution_deadline == claim.horizon.window_end
    checks.append(
        DraftCheck(
            check_id="D3",
            name="deadline_anti_drift",
            status="pass" if deadline_ok else "fail",
            detail=(
                f"deadline {validated.resolution_deadline.isoformat()} matches "
                "the claim window end"
                if deadline_ok
                else (
                    f"deadline {validated.resolution_deadline.isoformat()} drifted "
                    f"from claim window end {claim.horizon.window_end.isoformat()}"
                )
            ),
        )
    )

    # D4 — subject echo: declared subject is a real claim subject AND its
    # token appears in the draft prose (governed tok_v1; anti-mad-libs).
    text_tokens = tok_v1(validated.proposed_title) | tok_v1(
        validated.proposed_resolution_logic
    )
    declared_tokens = tok_v1(validated.subject_entity)
    subject_token_sets = [tok_v1(s) for s in _claim_subjects(claim)]
    declared_is_claim_subject = any(
        declared_tokens & st for st in subject_token_sets
    )
    appears_in_prose = bool(declared_tokens & text_tokens)
    subject_ok = declared_is_claim_subject and appears_in_prose
    if subject_ok:
        subject_detail = "claim subject named in the draft"
    elif not declared_is_claim_subject:
        subject_detail = (
            f"declared subject {validated.subject_entity!r} is not a claim "
            "subject"
        )
    else:
        subject_detail = (
            f"declared subject {validated.subject_entity!r} never appears in "
            "the draft title or resolution logic"
        )
    checks.append(
        DraftCheck(
            check_id="D4",
            name="subject_echo",
            status="pass" if subject_ok else "fail",
            detail=subject_detail,
        )
    )

    # D5 — resolution observability.
    observable = (
        validated.resolution_source_class is not ResolutionSourceClass.NONE
        and bool(validated.resolution_source.strip())
    )
    checks.append(
        DraftCheck(
            check_id="D5",
            name="resolution_observability",
            status="pass" if observable else "fail",
            detail=(
                "names a concrete resolution source"
                if observable
                else "no observable resolution source (class none or empty) — "
                "not a test"
            ),
        )
    )

    # D6 — object + metric echo: the draft must be about the claim's
    # OBJECT and what it MEASURES, not merely its subject. Catches a
    # shape-valid draft that drifts the thing being tested ("any product
    # update" for a "GPT-5 release" claim — subject OpenAI present, but
    # neither the object nor the release metric). Token-presence via the
    # governed tok_v1; this is NOT a full structural re-fit of the draft
    # against the claim (that — build a DraftMarketStructure and run the
    # fit gate — is the stronger, deferred Loop 4 option; D6 closes the
    # demonstrated hole without it).
    object_names = [e.name for e in claim.entities if e.role == "object"]
    object_tokens: set[str] = set()
    for name in object_names:
        object_tokens |= tok_v1(name)
    metric_tokens = tok_v1(claim.metric.what)
    object_ok = not object_names or bool(object_tokens & text_tokens)
    metric_ok = bool(metric_tokens & text_tokens)
    if object_ok and metric_ok:
        echo_detail = "claim object and metric echoed in the draft"
    elif not metric_ok:
        echo_detail = (
            f"draft does not echo the claim metric ({claim.metric.what!r})"
        )
    else:
        echo_detail = (
            f"draft does not echo the claim object "
            f"({', '.join(object_names)})"
        )
    checks.append(
        DraftCheck(
            check_id="D6",
            name="object_metric_echo",
            status="pass" if (object_ok and metric_ok) else "fail",
            detail=echo_detail,
        )
    )

    # D7 — event-stage match: the draft must resolve on the CLAIM's stage,
    # never a weaker proxy. Closes announce-vs-release drift (resolving on
    # "announced" for a "launched" claim is a different, easier event), the
    # exact analog of the fit gate's S1.
    stage_ok = validated.event_stage == claim.event_stage
    checks.append(
        DraftCheck(
            check_id="D7",
            name="event_stage_match",
            status="pass" if stage_ok else "fail",
            detail=(
                f"draft resolves on the claim's event stage "
                f"({claim.event_stage.value})"
                if stage_ok
                else f"draft resolves on {validated.event_stage.value} but the "
                f"claim's event stage is {claim.event_stage.value} — "
                "weaker-stage proxy"
            ),
        )
    )

    reasons = [c.detail for c in checks if c.status == "fail"]
    if reasons:
        return DraftGateResult(
            verdict=DraftGateVerdict.REJECTED_INVALID,
            checks=checks,
            reasons=reasons,
        )
    return DraftGateResult(
        verdict=DraftGateVerdict.PASS, draft=validated, checks=checks
    )
