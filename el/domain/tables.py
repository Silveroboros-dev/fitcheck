"""ORM models — blueprint §5 data model, verbatim.

Conventions:
- UUID primary keys generated app-side (uuid4);
- JSON columns use the generic JSON type with a JSONB variant on
  Postgres, so dev/tests run on SQLite and prod runs on Postgres;
- enum-valued columns store the StrEnum string (no native DB enums in
  Phase 1 — vocabulary changes are Loop 4 decisions, not migrations);
- conviction is event-sourced; everything else is conventional rows.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class _PK:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )


class _Created:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class User(_PK, _Created, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True)


class ApiClient(_PK, _Created, Base):
    __tablename__ = "api_clients"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    client_type: Mapped[str] = mapped_column(String(16))
    rate_limit_tier: Mapped[str] = mapped_column(String(32), default="default")
    # Revocation/expiry: a non-NULL timestamp disables the key, and
    # resolve_principal rejects it regardless of client_type. Modeled as an
    # audited timestamp (when it was revoked), matching the house style for
    # state — unlocked_at, odds_revealed_at — rather than a bare bool.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class McpUsageBucket(Base):
    """Bounded cross-instance usage counter for one API client and window.

    The stable scope names are overwritten when their fixed window rolls, so
    storage is bounded by ``api_clients * configured scopes`` rather than by
    request volume.  Production currently uses exactly two scopes.
    """

    __tablename__ = "mcp_usage_buckets"
    api_client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_clients.id", ondelete="CASCADE"), primary_key=True
    )
    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "used_units >= 0", name="ck_mcp_usage_units_nonnegative"
        ),
    )


class Job(_PK, _Created, Base):
    """Durable logical operation; queue delivery is never the source of truth.

    ``owner_client_type`` + ``owner_actor_id`` is the authorization scope for
    transient work.  ``owner_user_id`` is retained for quota/accounting only;
    two API keys for the same user do not thereby share classification jobs.
    System jobs use a system owner and leave the user/client FKs NULL.
    """

    __tablename__ = "jobs"
    job_type: Mapped[str] = mapped_column(String(64))
    owner_client_type: Mapped[str] = mapped_column(String(32))
    owner_actor_id: Mapped[str] = mapped_column(String(160))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id")
    )
    submitted_by_api_client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_clients.id")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload_hash_version: Mapped[int] = mapped_column(Integer, default=1)
    # Payloads should contain object references rather than duplicating source
    # text.  Model/policy/code versions are frozen separately at submission.
    payload: Mapped[dict] = mapped_column(JSONVariant)
    pinned_manifest: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    stage: Mapped[str | None] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    active_attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    result: Mapped[dict | None] = mapped_column(JSONVariant)
    error_kind: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Safe/public summary only. Raw provider/model exceptions belong in
    # redacted operator telemetry, never a client-visible durable row.
    safe_error_message: Mapped[str | None] = mapped_column(String(256))
    # Operator-only structured diagnostics. A future public status DTO must
    # exclude this field even when its current producer stores bounded data.
    error_details: Mapped[dict | None] = mapped_column(JSONVariant)
    correlation_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "owner_client_type",
            "owner_actor_id",
            "job_type",
            "idempotency_key",
            name="uq_job_idempotency",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_jobs_attempt_nonnegative"),
        CheckConstraint("max_attempts > 0", name="ck_jobs_max_attempts_positive"),
        CheckConstraint(
            "payload_hash_version > 0",
            name="ck_jobs_payload_hash_version_positive",
        ),
        CheckConstraint(
            "attempt_count <= max_attempts", name="ck_jobs_attempt_within_budget"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', "
            "'failed', 'cancelled', 'needs_operator')",
            name="ck_jobs_status",
        ),
        CheckConstraint(
            "((status = 'running' AND active_attempt_id IS NOT NULL "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND active_attempt_id IS NULL "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL))",
            name="ck_jobs_active_lease_shape",
        ),
        CheckConstraint(
            "((status IN ('succeeded', 'failed', 'cancelled', "
            "'needs_operator') AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('succeeded', 'failed', 'cancelled', "
            "'needs_operator') AND completed_at IS NULL))",
            name="ck_jobs_completion_shape",
        ),
        Index(
            "ix_jobs_claimable",
            "job_type",
            "status",
            "priority",
            "available_at",
        ),
        Index("ix_jobs_lease_expiry", "status", "lease_expires_at"),
        Index("ix_jobs_deadline", "status", "deadline_at"),
    )


class JobAttempt(_PK, Base):
    """One execution lease. Its UUID is the stale-worker fencing token."""

    __tablename__ = "job_attempts"
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    attempt_number: Mapped[int] = mapped_column(Integer)
    lease_owner: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="running")
    attempt_trace_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_kind: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    safe_error_message: Mapped[str | None] = mapped_column(String(256))
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
        CheckConstraint("attempt_number > 0", name="ck_job_attempt_number_positive"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'retry_wait', 'failed', "
            "'abandoned', 'cancelled')",
            name="ck_job_attempts_status",
        ),
        CheckConstraint(
            "((status = 'running' AND finished_at IS NULL) OR "
            "(status <> 'running' AND finished_at IS NOT NULL))",
            name="ck_job_attempts_completion_shape",
        ),
    )


class SourceSignal(_PK, Base):
    __tablename__ = "source_signals"
    source_type: Mapped[str] = mapped_column(String(32))
    source_actor: Mapped[str | None] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(String(2048))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class ThesisAnalysis(_PK, _Created, Base):
    __tablename__ = "thesis_analyses"
    source_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_signals.id")
    )
    input_text: Mapped[str] = mapped_column(Text)
    extracted_structure: Mapped[dict] = mapped_column(JSONVariant)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    normalized_claim_summary: Mapped[str] = mapped_column(Text)
    client_type: Mapped[str] = mapped_column(String(16))
    agent_client_id: Mapped[str | None] = mapped_column(String(128))


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    venue_id: Mapped[str] = mapped_column(String(64))
    as_of_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieval_id: Mapped[str] = mapped_column(String(128))


class MarketUniverseSnapshot(_Created, Base):
    """Validated, immutable full-universe artifact.

    This is deliberately separate from ``market_snapshots``.  The legacy
    table identifies bounded candidate/rules/price evidence, whereas this row
    identifies one shared provider metadata artifact.
    """

    __tablename__ = "market_universe_snapshots"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    venue: Mapped[str] = mapped_column(String(64))
    cutoff_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_sha256: Mapped[str] = mapped_column(String(64))
    membership_sha256: Mapped[str] = mapped_column(String(64))
    artifact_uri: Mapped[str] = mapped_column(String(2048))
    artifact_format: Mapped[str] = mapped_column(String(32))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    artifact_bytes: Mapped[int] = mapped_column(BigInteger)
    row_count: Mapped[int] = mapped_column(Integer)
    unique_market_count: Mapped[int] = mapped_column(Integer)
    open_market_count: Mapped[int] = mapped_column(Integer)
    normalization_policy_version: Mapped[str] = mapped_column(String(64))
    validation_policy_version: Mapped[str] = mapped_column(String(64))
    source_versions: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    manifest: Mapped[dict] = mapped_column(JSONVariant)
    created_by_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "venue",
            "cutoff_utc",
            "content_sha256",
            "normalization_policy_version",
            name="uq_market_universe_content",
        ),
        UniqueConstraint(
            "provider",
            "venue",
            "id",
            name="uq_market_universe_key_id",
        ),
        CheckConstraint("artifact_bytes >= 0", name="ck_universe_artifact_bytes"),
        CheckConstraint("row_count > 0", name="ck_universe_row_count"),
        CheckConstraint(
            "unique_market_count = row_count", name="ck_universe_unique_rows"
        ),
        CheckConstraint(
            "open_market_count >= 0 AND open_market_count <= row_count",
            name="ck_universe_open_count",
        ),
    )


class ActiveMarketUniverse(Base):
    """Atomic provider/venue pointer; failed builds never update this row."""

    __tablename__ = "active_market_universes"
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    venue: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(128))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    promoted_by_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    promoted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider", "venue", "snapshot_id"],
            [
                "market_universe_snapshots.provider",
                "market_universe_snapshots.venue",
                "market_universe_snapshots.id",
            ],
            name="fk_active_universe_snapshot_identity",
        ),
        CheckConstraint("generation > 0", name="ck_active_universe_generation"),
    )


class MarketRulesCapture(_PK, Base):
    __tablename__ = "market_rules_captures"
    market_id: Mapped[str] = mapped_column(String(128))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("market_snapshots.id"))
    contract_terms_text: Mapped[str] = mapped_column(Text)
    resolution_rules_text: Mapped[str] = mapped_column(Text)
    contract_terms_hash: Mapped[str] = mapped_column(String(64))
    resolution_rules_hash: Mapped[str] = mapped_column(String(64))
    rules_captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint("market_id", "snapshot_id", name="uq_rules_capture"),
    )


class MarketStructureRow(_PK, _Created, Base):
    """Cached by rules content, not by snapshot (ratification item 8).

    Identity = (market_id, rules hashes, schema_version, policy version);
    snapshot_id records where the structure was FIRST captured —
    provenance, not identity. A market re-seen with unchanged rules
    reuses its row; changed rules (new hash) force re-extraction.
    """

    __tablename__ = "market_structures"
    market_id: Mapped[str] = mapped_column(String(128))
    contract_terms_hash: Mapped[str] = mapped_column(String(64))
    resolution_rules_hash: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("market_snapshots.id"))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    structure: Mapped[dict] = mapped_column(JSONVariant)
    extraction_policy_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint(
            "market_id",
            "contract_terms_hash",
            "resolution_rules_hash",
            "schema_version",
            "extraction_policy_version",
            name="uq_market_structure",
        ),
    )


class CandidateSet(_PK, _Created, Base):
    __tablename__ = "candidate_sets"
    thesis_analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("thesis_analyses.id")
    )
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("market_snapshots.id"))
    # Claim-specific retrieval provenance belongs here, not on the shared
    # legacy market snapshot row.
    retrieval_id: Mapped[str | None] = mapped_column(String(128))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    __table_args__ = (UniqueConstraint("job_id", name="uq_candidate_set_job"),)


class CandidateSetMember(_PK, Base):
    __tablename__ = "candidate_set_members"
    candidate_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_sets.id")
    )
    market_id: Mapped[str] = mapped_column(String(128))
    rank: Mapped[int] = mapped_column(Integer)
    retrieval_score: Mapped[float | None] = mapped_column(Float)
    # Frozen market YES probability as of the retrieval snapshot (step 6).
    # ledger_entries.odds_at_entry is copied (thesis-side oriented) from here,
    # never re-fetched live — invariant #2 (frozen snapshots are eval truth).
    current_probability: Mapped[float | None] = mapped_column(Float)
    eligibility_flags: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    excluded_reason: Mapped[str | None] = mapped_column(String(256))
    __table_args__ = (
        UniqueConstraint(
            "candidate_set_id", "market_id", name="uq_candidate_member"
        ),
    )


class DraftContract(_PK, Base):
    __tablename__ = "draft_contracts"
    thesis_analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("thesis_analyses.id")
    )
    # Plain UUID, no FK constraint: breaks the DDL cycle
    # ledger_entries -> fit_cards -> draft_contracts -> ledger_entries.
    # Back-filled at save time (same pattern as conviction_events);
    # integrity enforced at the app layer.
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    proposed_title: Mapped[str] = mapped_column(String(512))
    proposed_resolution_logic: Mapped[str] = mapped_column(Text)
    resolution_source: Mapped[str] = mapped_column(String(256))
    category: Mapped[str | None] = mapped_column(String(64))
    time_horizon: Mapped[str | None] = mapped_column(String(64))
    # First-class gate-verified echo fields (migration 3496a0e487e3) — the
    # audit substance, not buried in the provenance blob. Nullable for a
    # trivial additive migration; the service always populates them.
    resolution_deadline: Mapped[date | None] = mapped_column(Date)
    resolution_source_class: Mapped[str | None] = mapped_column(String(16))
    subject_entity: Mapped[str | None] = mapped_column(String(256))
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("demand_clusters.id")
    )
    saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    # Complete-or-nothing run/policy provenance, like fit_cards and
    # market_recommendations (a draft is its own proposer call with its
    # own model_run_id). Migration ee90f333bae6.
    provenance: Mapped[dict] = mapped_column(JSONVariant)


class FitCard(_PK, _Created, Base):
    __tablename__ = "fit_cards"
    thesis_analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("thesis_analyses.id")
    )
    candidate_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_sets.id")
    )
    semantic_fit_class: Mapped[str] = mapped_column(String(32))
    recommended_market_id: Mapped[str | None] = mapped_column(String(128))
    what_it_captures: Mapped[str] = mapped_column(Text)
    what_it_misses: Mapped[str] = mapped_column(Text)
    horizon_match: Mapped[str] = mapped_column(String(8))
    resolution_risk: Mapped[str] = mapped_column(String(8))
    # Nullable (ratification item 9 amendment): a deterministic-fallback
    # card has NO calibrated confidence — NULL with
    # provenance.confidence_source, never a 0.5 sentinel.
    fit_confidence: Mapped[float | None] = mapped_column(Float)
    draft_contract_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("draft_contracts.id")
    )
    provenance: Mapped[dict] = mapped_column(JSONVariant)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    __table_args__ = (UniqueConstraint("job_id", name="uq_fit_card_job"),)


class MarketRecommendation(_PK, _Created, Base):
    __tablename__ = "market_recommendations"
    thesis_analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("thesis_analyses.id")
    )
    recommended_market_id: Mapped[str | None] = mapped_column(String(128))
    expression_type: Mapped[str] = mapped_column(String(32))
    # Mirrors fit_cards.fit_confidence: NULL when no calibrated source.
    fit_score: Mapped[float | None] = mapped_column(Float)
    fit_reason: Mapped[str] = mapped_column(Text)
    why_now: Mapped[str | None] = mapped_column(Text)
    crowding_note: Mapped[str | None] = mapped_column(Text)
    as_of_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("market_snapshots.id"))
    retrieval_id: Mapped[str] = mapped_column(String(128))
    venue_id: Mapped[str] = mapped_column(String(64))
    contract_terms_hash: Mapped[str | None] = mapped_column(String(64))
    resolution_rules_hash: Mapped[str | None] = mapped_column(String(64))
    rules_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    provenance: Mapped[dict] = mapped_column(JSONVariant)
    fit_card_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fit_cards.id")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    __table_args__ = (
        UniqueConstraint("fit_card_id", name="uq_recommendation_fit_card"),
        UniqueConstraint("job_id", name="uq_recommendation_job"),
    )


class RejectedMarketRow(_PK, Base):
    __tablename__ = "rejected_markets"
    market_recommendation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_recommendations.id")
    )
    market_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)


class LedgerEntry(_PK, _Created, Base):
    __tablename__ = "ledger_entries"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    thesis_analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("thesis_analyses.id")
    )
    thesis_summary: Mapped[str] = mapped_column(Text)
    user_justification: Mapped[str] = mapped_column(Text)
    linked_market_id: Mapped[str | None] = mapped_column(String(128))
    odds_at_entry: Mapped[float | None] = mapped_column(Float)
    # Side odds_at_entry was oriented to (P1 thesis_side): yes | no |
    # side_unknown. odds_at_entry is NULL when side_unknown, when no market is
    # linked (no_clean), or when the frozen member carries no price.
    odds_at_entry_side: Mapped[str | None] = mapped_column(String(16))
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("market_snapshots.id")
    )
    fit_class: Mapped[str] = mapped_column(String(32))
    fit_card_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fit_cards.id"))
    attestation_status: Mapped[str] = mapped_column(
        String(16), default="unattested"
    )
    client_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active")
    # One entry per (user, thesis) — backs create_ledger_entry's idempotency at
    # the DB level. The service's check-then-insert alone races: two concurrent
    # or retried saves both pass the existence check, then both insert.
    __table_args__ = (
        UniqueConstraint(
            "thesis_analysis_id", "user_id", name="uq_ledger_per_user_thesis"
        ),
    )


class ConvictionEvent(_PK, _Created, Base):
    __tablename__ = "conviction_events"
    thesis_analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("thesis_analyses.id")
    )
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ledger_entries.id")
    )
    conviction_level: Mapped[str | None] = mapped_column(String(16))
    intended_exposure_bucket: Mapped[str | None] = mapped_column(String(8))
    prior_probability: Mapped[float | None] = mapped_column(Float)
    prior_confidence: Mapped[str | None] = mapped_column(String(8))
    prior_reason: Mapped[str | None] = mapped_column(Text)
    prior_type: Mapped[str] = mapped_column(String(8))
    market_context_seen: Mapped[bool] = mapped_column(Boolean)
    prior_recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    odds_revealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    client_type: Mapped[str] = mapped_column(String(16))
    agent_client_id: Mapped[str | None] = mapped_column(String(128))


class AttestationEvent(_PK, _Created, Base):
    __tablename__ = "attestation_events"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    object_type: Mapped[str] = mapped_column(String(32))
    object_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str | None] = mapped_column(Text)


class DemandCluster(_PK, Base):
    __tablename__ = "demand_clusters"
    canonical_thesis: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64))
    cluster_size: Mapped[int] = mapped_column(Integer, default=0)
    aggregate_intended_exposure: Mapped[str | None] = mapped_column(String(32))
    latest_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class ReviewCandidate(_PK, _Created, Base):
    __tablename__ = "review_candidates"
    object_type: Mapped[str] = mapped_column(String(32))
    object_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    reviewer_notes: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class OddsLock(_PK, Base):
    """Blind-prior unlock bound to the AUTHENTICATED actor (review amendment),
    not the spoofable client_ref. Presence = unlocked; unlocked_at = when the
    blind prior landed. conviction_event_id is the blind event this lock
    unlocked — reveal stamps THAT event's odds_revealed_at, write-once.
    App-enforced link (no FK), mirroring draft_contracts.ledger_entry_id."""

    __tablename__ = "odds_locks"
    thesis_analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("thesis_analyses.id")
    )
    client_type: Mapped[str] = mapped_column(String(16))
    # Stringified authenticated identity: user_id (human_ui) / api_client_id
    # (api) / agent_client_id (agent_mcp). The security identity.
    actor_id: Mapped[str] = mapped_column(String(160))
    # Session / idempotency handle — NOT a security identity and NOT part of
    # the lock key. The lock is ACTOR-LEVEL (thesis + client_type + actor_id);
    # if client_ref were in the key an actor could switch it to mint a fresh
    # lock and bypass the blind-prior freeze (review blocker).
    client_ref: Mapped[str] = mapped_column(String(160))
    conviction_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    unlocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "thesis_analysis_id",
            "client_type",
            "actor_id",
            name="uq_odds_lock",
        ),
    )
