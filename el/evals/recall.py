"""Retrieval-recall eval harness — the Loop 2 CI gate (blueprint §8).

Loop 3 can classify perfectly and still fail if retrieval never surfaces
the right market; fit accuracy is meaningless downstream of a recall
miss. This harness runs golden claims through the REAL RetrievalService
(persistence included) against a frozen snapshot and measures whether
the judgment-relevant markets appear in the top-K eligible candidates.

Two metrics, one gate:
- recommended_recall — the known-good market for direct/indirect golden
  cases must appear in top-K. HARD GATE: 1.0 (CI fails otherwise).
- tempting_recall — the tempting markets the system must surface in
  order to reject them (weak/no-clean cases). REPORTED, not gated: the
  Phase 0 seed labels' rejected lists were authored for fit judgment,
  not retrieval relevance. The gate threshold for tempting_recall is
  fixed together with the EL-native golden set
  (market-fit-eval-set-requirements-v1).

Eval truth = frozen snapshots only (invariant #2). This module imports
the fixture provider ONLY; the live provider is structurally out of
reach and a CI test asserts that property over this file's source.
"""

import json
import sys
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.structures import ExtractedStructure
from el.domain.tables import Base, ThesisAnalysis
from el.retrieval.gate import GATE_POLICY_VERSION
from el.retrieval.provider import FixtureMarketProvider
from el.retrieval.ranking import RANKING_POLICY_VERSION
from el.retrieval.service import RetrievalService

RECALL_K_DEFAULT = 5


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecallCase(_Model):
    case_id: str
    structure: ExtractedStructure
    recommended_market_id: str | None = None
    tempting_market_ids: list[str] = []


class RecallCaseResult(_Model):
    case_id: str
    top_k_market_ids: list[str]
    recommended_market_id: str | None
    recommended_hit: bool | None
    tempting_market_ids: list[str]
    tempting_hits: int


class RecallReport(_Model):
    k: int
    snapshot_id: str
    cases: list[RecallCaseResult]
    recommended_total: int
    recommended_hits: int
    recommended_recall: float
    tempting_total: int
    tempting_hits: int
    tempting_recall: float
    passed: bool
    gate_policy_version: str = GATE_POLICY_VERSION
    ranking_policy_version: str = RANKING_POLICY_VERSION


def load_golden_cases(path: str | Path) -> list[RecallCase]:
    raw = json.loads(Path(path).read_text())
    return [RecallCase.model_validate(case) for case in raw["cases"]]


def run_recall_eval(
    *,
    golden_path: str | Path,
    snapshot_path: str | Path,
    k: int = RECALL_K_DEFAULT,
) -> RecallReport:
    cases = load_golden_cases(golden_path)
    provider = FixtureMarketProvider.from_path(snapshot_path)

    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    service = RetrievalService(provider, sessions)

    results: list[RecallCaseResult] = []
    snapshot_id = ""
    for case in cases:
        analysis_id = _insert_analysis(sessions, case)
        outcome = service.retrieve_candidates(analysis_id)
        snapshot_id = outcome.snapshot_id
        top_k = [
            candidate.market_id
            for candidate in outcome.candidates
            if candidate.eligible
        ][:k]
        recommended_hit = (
            case.recommended_market_id in top_k
            if case.recommended_market_id
            else None
        )
        results.append(
            RecallCaseResult(
                case_id=case.case_id,
                top_k_market_ids=top_k,
                recommended_market_id=case.recommended_market_id,
                recommended_hit=recommended_hit,
                tempting_market_ids=case.tempting_market_ids,
                tempting_hits=sum(
                    1 for mid in case.tempting_market_ids if mid in top_k
                ),
            )
        )

    recommended_total = sum(1 for r in results if r.recommended_market_id)
    recommended_hits = sum(1 for r in results if r.recommended_hit)
    tempting_total = sum(len(r.tempting_market_ids) for r in results)
    tempting_hits = sum(r.tempting_hits for r in results)
    recommended_recall = (
        recommended_hits / recommended_total if recommended_total else 1.0
    )
    return RecallReport(
        k=k,
        snapshot_id=snapshot_id,
        cases=results,
        recommended_total=recommended_total,
        recommended_hits=recommended_hits,
        recommended_recall=recommended_recall,
        tempting_total=tempting_total,
        tempting_hits=tempting_hits,
        tempting_recall=(
            tempting_hits / tempting_total if tempting_total else 1.0
        ),
        passed=recommended_recall == 1.0,
    )


def _insert_analysis(sessions, case: RecallCase) -> uuid.UUID:
    with sessions() as session:
        row = ThesisAnalysis(
            input_text=case.structure.claim_summary,
            extracted_structure=case.structure.model_dump(mode="json"),
            schema_version=case.structure.schema_version,
            normalized_claim_summary=case.structure.claim_summary,
            client_type="human_ui",
        )
        session.add(row)
        session.commit()
        return row.id


def main() -> int:
    golden = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/retrieval/recall_golden_phase0.json"
    snapshot = sys.argv[2] if len(sys.argv) > 2 else "tests/fixtures/retrieval/frozen_snapshot_phase0.json"
    report = run_recall_eval(golden_path=golden, snapshot_path=snapshot)
    print(report.model_dump_json(indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
