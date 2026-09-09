"""Ledger service (step 6): blind-prior odds lock, conviction capture, and
the strict ledger save — the Phase-1 exit where the loop closes to a saved
ledger entry.

Four operations, all deterministic bookkeeping over already-judged objects
(no model calls):

- submit_blind_prior: record a blind ConvictionEvent + an actor-scoped
  OddsLock (the unlock). Write-once after reveal: once odds have been revealed
  against the prior, it can no longer be changed.
- reveal_current_odds: agent/api surfaces require the lock (blind-prior
  protocol); the reveal stamps odds_revealed_at on the blind event (write-once,
  audited) and returns the recommended market's odds ORIENTED to the thesis
  side. human_ui reveals without a lock (the human A/B variant).
- create_ledger_entry: strict save -> save-time context ConvictionEvent ->
  back-fill ledger_entry_id on the thesis's conviction events -> odds_at_entry
  copied (thesis-side oriented) from the FROZEN candidate member, never
  re-fetched -> attestation by client_type -> draft back-link on no-clean.
  Idempotent per (user, thesis).
- get_ledger_entry / list_ledger_entries: read-back.

Odds locks are bound to the AUTHENTICATED actor identity (actor_id +
client_type), not the spoofable client_ref (review amendment); client_ref is
a session/idempotency handle inside that identity.
"""

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from el.domain.contracts import ConvictionEventIn, LedgerEntryOut
from el.domain.enums import (
    AttestationStatus,
    ClientType,
    ConvictionLevel,
    ExposureBucket,
    LedgerEntryStatus,
    PriorConfidence,
    PriorType,
)
from el.domain.tables import (
    CandidateSet,
    CandidateSetMember,
    ConvictionEvent,
    DraftContract,
    FitCard,
    LedgerEntry,
    OddsLock,
    ThesisAnalysis,
)
from el.ledger.odds import orient_odds, strict_save_violations

# Surfaces where the blind prior is protocol-enforced (spec v2): agents must
# pay the blind-prior toll before odds are revealed. human_ui is an open A/B.
_GATED_SURFACES = frozenset({ClientType.AGENT_MCP, ClientType.API})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BlindPriorOutcome(_Out):
    thesis_analysis_id: uuid.UUID
    conviction_event_id: uuid.UUID | None
    lock_id: uuid.UUID | None
    # created | updated | locked_after_reveal
    status: str
    reason: str = ""


class RevealOutcome(_Out):
    fit_card_id: uuid.UUID
    revealed: bool
    current_odds: float | None
    side: str
    odds_revealed_at: datetime | None
    reason: str = ""


class SaveOutcome(_Out):
    fit_card_id: uuid.UUID
    thesis_analysis_id: uuid.UUID
    saved: bool
    # saved | already_saved | rejected_incomplete
    status: str
    ledger_entry_id: uuid.UUID | None
    odds_at_entry: float | None = None
    odds_at_entry_side: str | None = None
    attestation_status: str | None = None
    violations: list[str] = []


class LedgerService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    # --- blind prior + odds lock --------------------------------------

    def submit_blind_prior(
        self,
        thesis_analysis_id: uuid.UUID,
        *,
        client_type: ClientType,
        actor_id: str,
        client_ref: str,
        prior_probability: float,
        prior_confidence: PriorConfidence | None = None,
        prior_reason: str | None = None,
        market_context_seen: bool = False,
        agent_client_id: str | None = None,
    ) -> BlindPriorOutcome:
        with self._sessions() as session:
            if session.get(ThesisAnalysis, thesis_analysis_id) is None:
                raise ValueError(
                    f"thesis_analysis {thesis_analysis_id} not found"
                )
            lock = self._find_lock(
                session, thesis_analysis_id, client_type, actor_id
            )
            if lock is not None and lock.conviction_event_id is not None:
                event = session.get(ConvictionEvent, lock.conviction_event_id)
                if event is not None and (
                    event.odds_revealed_at is not None
                    or event.ledger_entry_id is not None
                ):
                    # Write-once: the prior freezes once odds were revealed OR
                    # it was consumed by a ledger save (which itself reveals
                    # odds to the actor). A late re-submit cannot move it.
                    why = (
                        f"odds revealed at {event.odds_revealed_at.isoformat()}"
                        if event.odds_revealed_at is not None
                        else f"prior saved to ledger {event.ledger_entry_id}"
                    )
                    return BlindPriorOutcome(
                        thesis_analysis_id=thesis_analysis_id,
                        conviction_event_id=event.id,
                        lock_id=lock.id,
                        status="locked",
                        reason=f"blind prior is write-once; {why}",
                    )
                # Pre-reveal, pre-save revision is allowed.
                self._validate_blind(
                    thesis_analysis_id,
                    client_type,
                    prior_probability,
                    prior_confidence,
                    prior_reason,
                    market_context_seen,
                    agent_client_id,
                )
                event.prior_probability = prior_probability
                event.prior_confidence = (
                    prior_confidence.value if prior_confidence else None
                )
                event.prior_reason = prior_reason
                # Context exposure is monotonic audit truth. A later update may
                # record that an odds-free fit preview occurred, but it may
                # never erase an exposure already observed.
                event.market_context_seen = (
                    event.market_context_seen or market_context_seen
                )
                session.commit()
                return BlindPriorOutcome(
                    thesis_analysis_id=thesis_analysis_id,
                    conviction_event_id=event.id,
                    lock_id=lock.id,
                    status="updated",
                )

            self._validate_blind(
                thesis_analysis_id,
                client_type,
                prior_probability,
                prior_confidence,
                prior_reason,
                market_context_seen,
                agent_client_id,
            )
            event = ConvictionEvent(
                thesis_analysis_id=thesis_analysis_id,
                ledger_entry_id=None,
                conviction_level=None,
                intended_exposure_bucket=None,
                prior_probability=prior_probability,
                prior_confidence=(
                    prior_confidence.value if prior_confidence else None
                ),
                prior_reason=prior_reason,
                prior_type=PriorType.BLIND.value,
                market_context_seen=market_context_seen,
                odds_revealed_at=None,
                client_type=client_type.value,
                agent_client_id=agent_client_id,
            )
            session.add(event)
            session.flush()
            lock = OddsLock(
                thesis_analysis_id=thesis_analysis_id,
                client_type=client_type.value,
                actor_id=actor_id,
                client_ref=client_ref,
                conviction_event_id=event.id,
            )
            session.add(lock)
            session.commit()
            return BlindPriorOutcome(
                thesis_analysis_id=thesis_analysis_id,
                conviction_event_id=event.id,
                lock_id=lock.id,
                status="created",
            )

    def reveal_current_odds(
        self,
        fit_card_id: uuid.UUID,
        *,
        client_type: ClientType,
        actor_id: str,
        client_ref: str,
    ) -> RevealOutcome:
        with self._sessions() as session:
            card = session.get(FitCard, fit_card_id)
            if card is None:
                raise ValueError(f"fit_card {fit_card_id} not found")
            lock = self._find_lock(
                session,
                card.thesis_analysis_id,
                client_type,
                actor_id,
            )
            if client_type in _GATED_SURFACES and lock is None:
                return RevealOutcome(
                    fit_card_id=fit_card_id,
                    revealed=False,
                    current_odds=None,
                    side="side_unknown",
                    odds_revealed_at=None,
                    reason=(
                        "odds withheld: the agent surface must submit a blind "
                        "prior before odds are revealed"
                    ),
                )
            odds, side, linked_market_id, _ = self._recommended_odds(
                session, card
            )
            if linked_market_id is None:
                # No-clean (or no recommended market): nothing to reveal, and
                # side is None there — return gracefully, never crash.
                return RevealOutcome(
                    fit_card_id=fit_card_id,
                    revealed=False,
                    current_odds=None,
                    side="side_unknown",
                    odds_revealed_at=None,
                    reason="no linked market / no odds to reveal",
                )
            # Audited, write-once stamp on the blind event (first reveal wins).
            revealed_at: datetime | None = None
            if lock is not None and lock.conviction_event_id is not None:
                event = session.get(ConvictionEvent, lock.conviction_event_id)
                if event is not None:
                    if event.odds_revealed_at is None:
                        event.odds_revealed_at = _utcnow()
                        session.commit()
                    revealed_at = event.odds_revealed_at
            return RevealOutcome(
                fit_card_id=fit_card_id,
                revealed=True,
                current_odds=odds,
                side=side,
                odds_revealed_at=revealed_at,
            )

    # --- ledger save ---------------------------------------------------

    def create_ledger_entry(
        self,
        fit_card_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        client_type: ClientType,
        actor_id: str,
        client_ref: str,
        conviction_level: ConvictionLevel | None,
        intended_exposure_bucket: ExposureBucket | None,
        user_justification: str | None,
        agent_client_id: str | None = None,
    ) -> SaveOutcome:
        with self._sessions() as session:
            card = session.get(FitCard, fit_card_id)
            if card is None:
                raise ValueError(f"fit_card {fit_card_id} not found")
            thesis_analysis_id = card.thesis_analysis_id

            # Idempotency per (user, thesis): a second save returns the first.
            existing = session.scalars(
                select(LedgerEntry).where(
                    LedgerEntry.thesis_analysis_id == thesis_analysis_id,
                    LedgerEntry.user_id == user_id,
                )
            ).first()
            if existing is not None:
                return SaveOutcome(
                    fit_card_id=fit_card_id,
                    thesis_analysis_id=thesis_analysis_id,
                    saved=False,
                    status="already_saved",
                    ledger_entry_id=existing.id,
                    odds_at_entry=existing.odds_at_entry,
                    odds_at_entry_side=existing.odds_at_entry_side,
                    attestation_status=existing.attestation_status,
                )

            requires_blind = client_type in _GATED_SURFACES
            lock = self._find_lock(
                session, thesis_analysis_id, client_type, actor_id
            )
            violations = strict_save_violations(
                conviction_level=conviction_level,
                intended_exposure_bucket=intended_exposure_bucket,
                user_justification=user_justification,
                requires_blind_prior=requires_blind,
                blind_prior_present=lock is not None,
            )
            if violations:
                return SaveOutcome(
                    fit_card_id=fit_card_id,
                    thesis_analysis_id=thesis_analysis_id,
                    saved=False,
                    status="rejected_incomplete",
                    ledger_entry_id=None,
                    violations=violations,
                )

            odds, side, linked_market_id, snapshot_id = self._recommended_odds(
                session, card
            )
            attestation = (
                AttestationStatus.ATTESTED
                if client_type is ClientType.HUMAN_UI
                else AttestationStatus.UNATTESTED
            )
            analysis = session.get(ThesisAnalysis, thesis_analysis_id)
            entry = LedgerEntry(
                user_id=user_id,
                thesis_analysis_id=thesis_analysis_id,
                thesis_summary=analysis.normalized_claim_summary,
                user_justification=user_justification,
                linked_market_id=linked_market_id,
                odds_at_entry=odds,
                odds_at_entry_side=side,
                snapshot_id=snapshot_id,
                fit_class=card.semantic_fit_class,
                fit_card_id=card.id,
                attestation_status=attestation.value,
                client_type=client_type.value,
                status=LedgerEntryStatus.ACTIVE.value,
            )
            session.add(entry)
            try:
                session.flush()
            except IntegrityError:
                # A concurrent/retried save for the same (user, thesis) landed
                # between the idempotency check above and this insert;
                # uq_ledger_per_user_thesis caught it. Roll back and return the
                # winner instead of surfacing the constraint error.
                session.rollback()
                existing = session.scalars(
                    select(LedgerEntry).where(
                        LedgerEntry.thesis_analysis_id == thesis_analysis_id,
                        LedgerEntry.user_id == user_id,
                    )
                ).first()
                if existing is None:
                    raise
                return SaveOutcome(
                    fit_card_id=fit_card_id,
                    thesis_analysis_id=thesis_analysis_id,
                    saved=False,
                    status="already_saved",
                    ledger_entry_id=existing.id,
                    odds_at_entry=existing.odds_at_entry,
                    odds_at_entry_side=existing.odds_at_entry_side,
                    attestation_status=existing.attestation_status,
                )

            # Save-time conviction event (context; carries conviction+exposure).
            self._validate_context_save(
                thesis_analysis_id,
                client_type,
                conviction_level,
                intended_exposure_bucket,
                agent_client_id,
            )
            save_event = ConvictionEvent(
                thesis_analysis_id=thesis_analysis_id,
                ledger_entry_id=entry.id,
                conviction_level=conviction_level.value,
                intended_exposure_bucket=intended_exposure_bucket.value,
                prior_probability=None,
                prior_confidence=None,
                prior_reason=None,
                prior_type=PriorType.CONTEXT.value,
                market_context_seen=True,
                odds_revealed_at=_utcnow() if linked_market_id else None,
                client_type=client_type.value,
                agent_client_id=agent_client_id,
            )
            session.add(save_event)

            # Back-fill the blind prior (if any) onto this ledger entry — the
            # stream stays recoverable; the blind event is never flattened.
            # Saving an odds-bearing entry reveals odds to the actor, so freeze
            # the prior write-once even when reveal_current_odds was skipped.
            if lock is not None and lock.conviction_event_id is not None:
                blind = session.get(ConvictionEvent, lock.conviction_event_id)
                if blind is not None:
                    blind.ledger_entry_id = entry.id
                    if linked_market_id is not None and (
                        blind.odds_revealed_at is None
                    ):
                        blind.odds_revealed_at = _utcnow()

            # No-clean: link the draft contract back to this entry.
            if card.draft_contract_id is not None:
                draft = session.get(DraftContract, card.draft_contract_id)
                if draft is not None:
                    draft.ledger_entry_id = entry.id

            session.commit()
            return SaveOutcome(
                fit_card_id=fit_card_id,
                thesis_analysis_id=thesis_analysis_id,
                saved=True,
                status="saved",
                ledger_entry_id=entry.id,
                odds_at_entry=odds,
                odds_at_entry_side=side,
                attestation_status=attestation.value,
            )

    # --- read-back -----------------------------------------------------

    def has_blind_prior(
        self,
        thesis_analysis_id: uuid.UUID,
        *,
        client_type: ClientType,
        actor_id: str,
    ) -> bool:
        """Read accessor: does this actor hold a blind-prior lock for the
        thesis? Lets the MCP boundary distinguish "no prior" (-> require one)
        from "no-clean, no odds to reveal" — both of which reveal_current_odds
        reports as revealed=False. Exposes existing lock state; no new logic."""
        with self._sessions() as session:
            return (
                self._find_lock(session, thesis_analysis_id, client_type, actor_id)
                is not None
            )

    def get_ledger_entry(self, ledger_entry_id: uuid.UUID) -> LedgerEntryOut:
        with self._sessions() as session:
            entry = session.get(LedgerEntry, ledger_entry_id)
            if entry is None:
                raise ValueError(f"ledger_entry {ledger_entry_id} not found")
            return LedgerEntryOut.model_validate(entry)

    def list_ledger_entries(
        self, user_id: uuid.UUID
    ) -> list[LedgerEntryOut]:
        with self._sessions() as session:
            rows = session.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.user_id == user_id)
                .order_by(LedgerEntry.created_at.desc())
            ).all()
            return [LedgerEntryOut.model_validate(r) for r in rows]

    # --- internals -----------------------------------------------------

    def _find_lock(
        self,
        session: Session,
        thesis_analysis_id: uuid.UUID,
        client_type: ClientType,
        actor_id: str,
    ) -> OddsLock | None:
        # ACTOR-LEVEL identity: thesis + client_type + actor_id. client_ref is
        # session/idempotency metadata and is deliberately NOT part of the
        # lookup — keying on it would let one actor switch client_ref to mint a
        # fresh lock and bypass the blind-prior freeze (review blocker).
        return session.scalars(
            select(OddsLock).where(
                OddsLock.thesis_analysis_id == thesis_analysis_id,
                OddsLock.client_type == client_type.value,
                OddsLock.actor_id == actor_id,
            )
        ).first()

    def _recommended_odds(
        self, session: Session, card: FitCard
    ) -> tuple[float | None, str | None, str | None, str | None]:
        """(odds_at_entry, side, linked_market_id, snapshot_id) for the card's
        recommended market, oriented to thesis_side, from the FROZEN candidate
        member only. No recommended market (no_clean) -> all-None odds."""
        candidate_set = session.get(CandidateSet, card.candidate_set_id)
        snapshot_id = candidate_set.snapshot_id if candidate_set else None
        recommended = card.recommended_market_id
        if recommended is None:
            return None, None, None, snapshot_id
        member = session.scalars(
            select(CandidateSetMember).where(
                CandidateSetMember.candidate_set_id == card.candidate_set_id,
                CandidateSetMember.market_id == recommended,
            )
        ).first()
        current_probability = member.current_probability if member else None
        thesis_side = (card.provenance or {}).get("thesis_side")
        odds, side = orient_odds(current_probability, thesis_side)
        return odds, side, recommended, snapshot_id

    def _validate_blind(
        self,
        thesis_analysis_id: uuid.UUID,
        client_type: ClientType,
        prior_probability: float,
        prior_confidence: PriorConfidence | None,
        prior_reason: str | None,
        market_context_seen: bool,
        agent_client_id: str | None,
    ) -> None:
        # Reuse the binding temporal rules (contracts.ConvictionEventIn):
        # blind => odds_revealed_at None and probability set. An MCP preview may
        # have exposed fit context without exposing odds; preserve that fact.
        ConvictionEventIn(
            thesis_analysis_id=thesis_analysis_id,
            prior_type=PriorType.BLIND,
            market_context_seen=market_context_seen,
            odds_revealed_at=None,
            prior_probability=prior_probability,
            prior_confidence=prior_confidence,
            prior_reason=prior_reason,
            conviction_level=None,
            intended_exposure_bucket=None,
            client_type=client_type,
            agent_client_id=agent_client_id,
        )

    def _validate_context_save(
        self,
        thesis_analysis_id: uuid.UUID,
        client_type: ClientType,
        conviction_level: ConvictionLevel,
        intended_exposure_bucket: ExposureBucket,
        agent_client_id: str | None,
    ) -> None:
        ConvictionEventIn(
            thesis_analysis_id=thesis_analysis_id,
            prior_type=PriorType.CONTEXT,
            market_context_seen=True,
            odds_revealed_at=None,
            prior_probability=None,
            conviction_level=conviction_level,
            intended_exposure_bucket=intended_exposure_bucket,
            client_type=client_type,
            agent_client_id=agent_client_id,
        )
