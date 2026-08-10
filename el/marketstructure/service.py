"""Market-structure service: cache lookup -> propose -> gate -> persist.

Cost-control core (ratification item 8): extraction runs ONCE per unique
rules content. Cache identity is (market_id, contract_terms_hash,
resolution_rules_hash, schema_version, extraction_policy_version); the
hashes come from the rules captures persisted at retrieval time, so a
market re-seen across snapshots with unchanged rules never re-extracts.

Operates on a candidate set (Loop 2 output): eligible members only —
ineligible candidates never reach Loop 3, so extracting them would be
spend without a reader.
"""

import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.structures import MarketStructure
from el.domain.tables import (
    CandidateSet,
    CandidateSetMember,
    MarketRulesCapture,
    MarketStructureRow,
)
from el.marketstructure.gate import (
    GATE_POLICY_VERSION,
    MarketGateVerdict,
    market_structure_gate,
)
from el.models.market_adapter import (
    MARKET_EXTRACTION_POLICY_VERSION,
    MarketStructureProposer,
)

SCHEMA_VERSION = 1


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RejectedProposal(_Out):
    market_id: str
    reasons: list[str]
    model_adapter: str
    model_run_id: str


class StructureOutcome(_Out):
    candidate_set_id: uuid.UUID
    snapshot_id: str
    structures: list[MarketStructure]
    cache_hits: int
    extracted: int
    rejected: list[RejectedProposal]
    skipped_ineligible: int
    # Funnel metrics for the top-N cap. The retrieved universe / eligible set
    # can be large; structured_count (hence Gemini calls) is bounded by
    # structure_cap. skipped_unstructured_count = eligible markets the cap kept
    # out of extraction (still persisted as candidate-set members, never
    # structured). structure_cap is None when unbounded.
    retrieved_count: int
    eligible_count: int
    structured_count: int
    skipped_unstructured_count: int
    structure_cap: int | None
    gate_policy_version: str = GATE_POLICY_VERSION
    extraction_policy_version: int = MARKET_EXTRACTION_POLICY_VERSION


class MarketStructureService:
    def __init__(
        self,
        proposer: MarketStructureProposer,
        session_factory: sessionmaker[Session],
        *,
        top_n: int | None = None,
    ):
        self._proposer = proposer
        self._sessions = session_factory
        # Top-N structure-extraction cap. None = unbounded (the test/fixture
        # default, byte-identical to pre-cap behaviour). Production wiring
        # injects a small positive cap (MARKET_STRUCTURE_TOP_N, default 15) so
        # structured_count and Gemini calls stay bounded over a live universe
        # of thousands of markets.
        self._top_n = top_n

    def ensure_structures(
        self, candidate_set_id: uuid.UUID, *, top_n_override: int | None = None
    ) -> StructureOutcome:
        with self._sessions() as session:
            candidate_set = session.get(CandidateSet, candidate_set_id)
            if candidate_set is None:
                raise ValueError(f"candidate_set {candidate_set_id} not found")
            snapshot_id = candidate_set.snapshot_id
            members = (
                session.scalars(
                    select(CandidateSetMember)
                    .where(
                        CandidateSetMember.candidate_set_id == candidate_set_id
                    )
                    .order_by(CandidateSetMember.rank)
                )
            ).all()

            retrieved_count = len(members)
            eligible = [m for m in members if m.excluded_reason is None]
            eligible_count = len(eligible)
            skipped_ineligible = retrieved_count - eligible_count

            # Top-N cap: structure only the highest-ranked eligible candidates.
            # The cheap Loop-2 ranker already ordered the members; lower-ranked
            # eligibles stay persisted as candidate members but are never
            # structured, so Gemini calls and structured_count never scale with
            # the (potentially huge) retrieved universe.
            top_n = top_n_override if top_n_override is not None else self._top_n
            if top_n is not None and eligible_count > top_n:
                to_structure = eligible[:top_n]
            else:
                to_structure = eligible
            skipped_unstructured_count = eligible_count - len(to_structure)

            structures: list[MarketStructure] = []
            rejected: list[RejectedProposal] = []
            cache_hits = extracted = 0

            for member in to_structure:
                capture = self._rules_capture(
                    session, member.market_id, snapshot_id
                )
                cached = self._cache_lookup(session, member.market_id, capture)
                if cached is not None:
                    cache_hits += 1
                    # The DB row's snapshot_id is FIRST-capture provenance
                    # (ratification item 8); stamp the current request
                    # snapshot on the runtime object so downstream verdicts
                    # never carry a stale snapshot identity. Same discipline
                    # as the fixture/Gemini proposers, which stamp identity
                    # from the request.
                    structure = MarketStructure.model_validate(
                        cached.structure
                    ).model_copy(update={"snapshot_id": snapshot_id})
                    structures.append(structure)
                    continue

                proposed = self._proposer.propose_market_structure(
                    market_id=member.market_id,
                    snapshot_id=snapshot_id,
                    contract_terms_text=capture.contract_terms_text,
                    resolution_rules_text=capture.resolution_rules_text,
                )
                result = market_structure_gate(
                    market_id=member.market_id,
                    snapshot_id=snapshot_id,
                    structure=proposed.structure,
                )
                if result.verdict is not MarketGateVerdict.PASS:
                    rejected.append(
                        RejectedProposal(
                            market_id=member.market_id,
                            reasons=result.reasons,
                            model_adapter=proposed.model_adapter,
                            model_run_id=proposed.model_run_id,
                        )
                    )
                    continue

                assert result.structure is not None
                session.add(
                    MarketStructureRow(
                        market_id=member.market_id,
                        contract_terms_hash=capture.contract_terms_hash,
                        resolution_rules_hash=capture.resolution_rules_hash,
                        snapshot_id=snapshot_id,
                        schema_version=SCHEMA_VERSION,
                        structure=result.structure.model_dump(mode="json"),
                        extraction_policy_version=(
                            MARKET_EXTRACTION_POLICY_VERSION
                        ),
                    )
                )
                extracted += 1
                structures.append(result.structure)

            session.commit()

        return StructureOutcome(
            candidate_set_id=candidate_set_id,
            snapshot_id=snapshot_id,
            structures=structures,
            cache_hits=cache_hits,
            extracted=extracted,
            rejected=rejected,
            skipped_ineligible=skipped_ineligible,
            retrieved_count=retrieved_count,
            eligible_count=eligible_count,
            structured_count=cache_hits + extracted,
            skipped_unstructured_count=skipped_unstructured_count,
            structure_cap=top_n,
        )

    def _rules_capture(
        self, session: Session, market_id: str, snapshot_id: str
    ) -> MarketRulesCapture:
        capture = session.execute(
            select(MarketRulesCapture).where(
                MarketRulesCapture.market_id == market_id,
                MarketRulesCapture.snapshot_id == snapshot_id,
            )
        ).scalar_one_or_none()
        if capture is None:
            raise ValueError(
                f"no rules capture for ({market_id}, {snapshot_id}) — "
                "retrieval must run before structure extraction"
            )
        return capture

    def _cache_lookup(
        self,
        session: Session,
        market_id: str,
        capture: MarketRulesCapture,
    ) -> MarketStructureRow | None:
        return session.execute(
            select(MarketStructureRow).where(
                MarketStructureRow.market_id == market_id,
                MarketStructureRow.contract_terms_hash
                == capture.contract_terms_hash,
                MarketStructureRow.resolution_rules_hash
                == capture.resolution_rules_hash,
                MarketStructureRow.schema_version == SCHEMA_VERSION,
                MarketStructureRow.extraction_policy_version
                == MARKET_EXTRACTION_POLICY_VERSION,
            )
        ).scalar_one_or_none()
