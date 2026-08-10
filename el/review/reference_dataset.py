"""Reviewed reference dataset v0.

This module owns the local JSONL contract for review packets, market snapshots,
advisory candidate judgments, human review decisions, and derived experiment
exports. It is intentionally model-free; model outputs arrive only as advisory
candidate judgments.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


DATASET_VERSION = "review_dataset_v0"
CLEAN_GOLDEN_DATASET_VERSION = "clean_goldens_v1"
CLEAN_GOLDEN_CRITERIA_VERSION = "clean-golden-criteria-v1"
REVIEWED_FIT_TESTING_DATASET_VERSION = "reviewed_fit_testing_v1"

FIT_CLASSES = {"direct", "indirect", "weak_proxy", "no_clean_expression"}
SOURCE_SCOPES = {
    "real_world_reference_candidate",
    "source_text_and_provenance_only",
    "synthetic_stress",
    "fitcheck_regression",
    "live_candidate",
}
REVIEW_STATES = {
    "unreviewed",
    "single_reviewed",
    "confirmed_reference",
    "disputed",
    "candidate_only",
    "rejected",
}
LABEL_DECISIONS = {"keep", "change", "disputed", "reject_case"}
CONFIDENCE_VALUES = {"high", "medium", "low"}
CASE_QUALITY_VALUES = {
    "usable",
    "insufficient_context",
    "stale",
    "duplicate",
    "bad_market_snapshot",
    "reject",
}
RECOMMENDATION_VALUES = {
    "include_reference",
    "include_stress",
    "candidate_only",
    "needs_second_review",
    "reject",
}
PREFERENCE_STRENGTH_VALUES = {"strong", "weak", "tie", "unclear", "none"}
MARKET_SNAPSHOT_SELECTION_VALUES = {"selected", "none"}
RECHECK_STATUS_VALUES = {"not_rechecked", "cleared"}
REQUIRED_REVIEW_TEXT_FIELDS = {
    "packet_id": "packet",
    "reviewer_id": "reviewer",
    "notes": "notes",
}
MARKET_ROLES = {
    "best",
    "acceptable",
    "rejected",
    "tempting",
    "candidate",
    "stress_embedded",
    "other",
}
REVIEW_NOTE_REJECT_RE = re.compile(r"\breject(?:ed|ing|ion)?\b", re.IGNORECASE)

APPROVED_TAXONOMY_TERMS = [
    "no_failure",
    "event_stage_mismatch",
    "metric_mismatch",
    "temporal_metric_mismatch",
    "horizon_mismatch",
    "entity_mismatch",
    "causal_mechanism_mismatch",
    "inverse_or_polarity_mismatch",
    "composite_condition_mismatch",
    "weak_proxy_confound",
    "no_clean_expression",
    "over_strong_false_positive",
    "conservative_undercall",
    "retrieval_wrong_market",
    "market_rules_ambiguous",
    "source_context_insufficient",
    "duplicate_or_stale_case",
]


class ReviewDataError(ValueError):
    """Raised for invalid review data or unsafe review operations."""


class Taxonomy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["review_taxonomy_v0"] = "review_taxonomy_v0"
    approved_terms: list[str] = Field(default_factory=lambda: APPROVED_TAXONOMY_TERMS.copy())
    export_blocked_terms: list[str] = Field(default_factory=lambda: ["other"])
    aliases: dict[str, str] = Field(default_factory=dict)

    def normalize_terms(self, terms: Iterable[str]) -> list[str]:
        aliases = {key.lower(): value for key, value in self.aliases.items()}
        normalized: list[str] = []
        for term in terms:
            cleaned = term.strip()
            if not cleaned:
                continue
            canonical = aliases.get(cleaned.lower(), cleaned)
            normalized.append(canonical)
        return normalized

    def validate_terms(self, terms: Iterable[str]) -> None:
        approved = set(self.approved_terms)
        blocked = set(self.export_blocked_terms)
        invalid = [
            term
            for term in self.normalize_terms(terms)
            if term not in approved and term not in blocked
        ]
        if invalid:
            raise ReviewDataError(f"unapproved taxonomy terms: {invalid}")


class ReviewPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["review_packet_v0"] = "review_packet_v0"
    dataset_version: Literal["review_dataset_v0"] = DATASET_VERSION
    packet_id: str
    source_dataset: str
    source_case_id: str
    source_scope: str
    source_row_hash: str
    source_text: str
    source_provenance: dict[str, Any] = Field(default_factory=dict)
    normalized_claim: dict[str, Any] = Field(default_factory=dict)
    reference_candidate: dict[str, Any] = Field(default_factory=dict)
    market_snapshot_ids: list[str] = Field(default_factory=list)
    candidate_judgment_ids: list[str] = Field(default_factory=list)
    priority: int = 0
    review_state: str = "unreviewed"

    @field_validator("source_scope")
    @classmethod
    def _source_scope_valid(cls, value: str) -> str:
        if value not in SOURCE_SCOPES:
            raise ValueError(f"invalid source_scope: {value}")
        return value

    @field_validator("review_state")
    @classmethod
    def _review_state_valid(cls, value: str) -> str:
        if value not in REVIEW_STATES:
            raise ValueError(f"invalid review_state: {value}")
        return value


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["market_snapshot_v0"] = "market_snapshot_v0"
    market_snapshot_id: str
    market_id: str
    venue: str
    url: str | None = None
    title: str
    description: str = ""
    resolution_rules: str = ""
    outcomes: list[str] = Field(default_factory=list)
    close_date: str | None = None
    status: str = "unknown"
    captured_at_utc: str
    snapshot_kind: str = "fixture"
    retrieval_provider: str = "fixture"
    retrieval_id: str | None = None
    retrieval_query: str | None = None
    polydata_cutoff_at_utc: str | None = None
    market_role: str = "candidate"

    @field_validator("market_role")
    @classmethod
    def _market_role_valid(cls, value: str) -> str:
        if value not in MARKET_ROLES:
            raise ValueError(f"invalid market_role: {value}")
        return value


class CandidateJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["candidate_judgment_v0"] = "candidate_judgment_v0"
    candidate_judgment_id: str
    packet_id: str
    arm_id: str
    generated_at_utc: str
    model: str
    prompt_version: str
    fit_class: str
    recommended_market_id: str | None = None
    rationale: str = ""
    false_strong: bool = False
    raw_output_ref: str | None = None

    @field_validator("fit_class")
    @classmethod
    def _fit_class_valid(cls, value: str) -> str:
        if value not in FIT_CLASSES:
            raise ValueError(f"invalid fit_class: {value}")
        return value


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["review_decision_v0"] = "review_decision_v0"
    review_decision_id: str
    packet_id: str
    reviewer_id: str
    reviewed_at_utc: str
    label_decision: str
    fit_class: str
    confidence: str
    case_quality: str
    failure_taxonomy: list[str] = Field(default_factory=list)
    false_strong_risk: bool = False
    market_snapshot_selection: str = "selected"
    selected_market_snapshot_id: str | None = None
    preferred_judgment_id: str | None = None
    preference_strength: str = "none"
    recommendation: str
    notes: str = ""
    recheck_status: str = "not_rechecked"
    rechecked_at_utc: str | None = None
    recheck_mode: str | None = None

    @field_validator("label_decision")
    @classmethod
    def _label_decision_valid(cls, value: str) -> str:
        if value not in LABEL_DECISIONS:
            raise ValueError(f"invalid label_decision: {value}")
        return value

    @field_validator("fit_class")
    @classmethod
    def _fit_class_valid(cls, value: str) -> str:
        if value not in FIT_CLASSES:
            raise ValueError(f"invalid fit_class: {value}")
        return value

    @field_validator("confidence")
    @classmethod
    def _confidence_valid(cls, value: str) -> str:
        if value not in CONFIDENCE_VALUES:
            raise ValueError(f"invalid confidence: {value}")
        return value

    @field_validator("case_quality")
    @classmethod
    def _case_quality_valid(cls, value: str) -> str:
        if value not in CASE_QUALITY_VALUES:
            raise ValueError(f"invalid case_quality: {value}")
        return value

    @field_validator("recommendation")
    @classmethod
    def _recommendation_valid(cls, value: str) -> str:
        if value not in RECOMMENDATION_VALUES:
            raise ValueError(f"invalid recommendation: {value}")
        return value

    @field_validator("preference_strength")
    @classmethod
    def _preference_strength_valid(cls, value: str) -> str:
        if value not in PREFERENCE_STRENGTH_VALUES:
            raise ValueError(f"invalid preference_strength: {value}")
        return value

    @field_validator("market_snapshot_selection")
    @classmethod
    def _market_snapshot_selection_valid(cls, value: str) -> str:
        if value not in MARKET_SNAPSHOT_SELECTION_VALUES:
            raise ValueError(f"invalid market_snapshot_selection: {value}")
        return value

    @field_validator("recheck_status")
    @classmethod
    def _recheck_status_valid(cls, value: str) -> str:
        if value not in RECHECK_STATUS_VALUES:
            raise ValueError(f"invalid recheck_status: {value}")
        return value


class ReviewDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packet_id: str
    reviewer_id: str
    label_decision: str
    fit_class: str
    confidence: str
    case_quality: str
    failure_taxonomy: list[str] = Field(default_factory=list)
    false_strong_risk: bool = False
    market_snapshot_selection: str = "selected"
    selected_market_snapshot_id: str | None = None
    preferred_judgment_id: str | None = None
    preference_strength: str = "none"
    recommendation: str
    notes: str = ""

    @field_validator("market_snapshot_selection")
    @classmethod
    def _market_snapshot_selection_valid(cls, value: str) -> str:
        if value not in MARKET_SNAPSHOT_SELECTION_VALUES:
            raise ValueError(f"invalid market_snapshot_selection: {value}")
        return value


class MarketSnapshotEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_market_snapshot_id: str | None = None
    market_id: str
    venue: str = "Polymarket"
    url: str
    title: str
    description: str = ""
    resolution_rules: str
    outcomes: list[str] = Field(default_factory=lambda: ["Yes", "No"])
    close_date: str | None = None
    status: str = "open"
    market_role: str = "candidate"

    @field_validator("market_id", "venue", "url", "title", "resolution_rules")
    @classmethod
    def _required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field cannot be blank")
        return cleaned

    @field_validator("market_role")
    @classmethod
    def _market_role_valid(cls, value: str) -> str:
        if value not in MARKET_ROLES:
            raise ValueError(f"invalid market_role: {value}")
        return value

    @field_validator("outcomes")
    @classmethod
    def _outcomes_not_empty(cls, value: list[str]) -> list[str]:
        outcomes = [item.strip() for item in value if item.strip()]
        if not outcomes:
            raise ValueError("outcomes cannot be empty")
        return outcomes


def validate_review_input_completeness(payload: ReviewDecisionInput) -> None:
    """Reject incomplete reviewer input before it becomes append-only data."""
    missing = [
        label
        for field, label in REQUIRED_REVIEW_TEXT_FIELDS.items()
        if not str(getattr(payload, field) or "").strip()
    ]
    if not payload.failure_taxonomy:
        missing.append("failure_taxonomy")
    if missing:
        raise ReviewDataError(f"missing required review fields: {', '.join(missing)}")

    if payload.preferred_judgment_id and payload.preference_strength not in {"strong", "weak"}:
        raise ReviewDataError(
            "preference_strength must be strong or weak when preferred_judgment_id is set"
        )
    if (
        not payload.preferred_judgment_id
        and payload.preference_strength in {"strong", "weak"}
    ):
        raise ReviewDataError(
            "preferred_judgment_id is required when preference_strength is strong or weak"
        )


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(data: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: Iterable[BaseModel | dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in rows:
        data = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
        lines.append(canonical_json(data))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def append_jsonl(path: Path, row: BaseModel | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(data) + "\n")


def load_taxonomy(path: Path) -> Taxonomy:
    return Taxonomy.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_packets(path: Path) -> list[ReviewPacket]:
    return [ReviewPacket.model_validate(row) for row in read_jsonl(path)]


def load_snapshots(path: Path) -> list[MarketSnapshot]:
    return [MarketSnapshot.model_validate(row) for row in read_jsonl(path)]


def load_candidate_judgments(path: Path) -> list[CandidateJudgment]:
    return [CandidateJudgment.model_validate(row) for row in read_jsonl(path)]


def load_decisions(path: Path, taxonomy: Taxonomy | None = None) -> list[ReviewDecision]:
    decisions = [ReviewDecision.model_validate(row) for row in read_jsonl(path)]
    if taxonomy is not None:
        for decision in decisions:
            taxonomy.validate_terms(decision.failure_taxonomy)
    return decisions


def latest_decisions_by_packet(
    decisions: Iterable[ReviewDecision],
) -> dict[str, ReviewDecision]:
    latest: dict[str, ReviewDecision] = {}
    for decision in sorted(
        decisions, key=lambda d: (d.packet_id, d.reviewed_at_utc, d.review_decision_id)
    ):
        latest[decision.packet_id] = decision
    return latest


def latest_decisions_by_reviewer(
    decisions: Iterable[ReviewDecision],
) -> dict[tuple[str, str], ReviewDecision]:
    latest: dict[tuple[str, str], ReviewDecision] = {}
    for decision in sorted(
        decisions,
        key=lambda d: (
            d.packet_id,
            d.reviewer_id,
            d.reviewed_at_utc,
            d.review_decision_id,
        ),
    ):
        latest[(decision.packet_id, decision.reviewer_id)] = decision
    return latest


def proposed_thesis_text(packet: ReviewPacket) -> str:
    normalized = packet.normalized_claim or {}
    for key in ("claim_text", "summary", "thesis"):
        value = normalized.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def review_readiness_warnings(packet: ReviewPacket) -> list[str]:
    warnings: list[str] = []
    if not packet.market_snapshot_ids:
        warnings.append("missing_market_snapshots")
    if not proposed_thesis_text(packet):
        warnings.append("missing_proposed_thesis")
    return warnings


def is_decision_ready_packet(packet: ReviewPacket) -> bool:
    return not review_readiness_warnings(packet)


def validate_references(
    *,
    packets: list[ReviewPacket],
    snapshots: list[MarketSnapshot],
    judgments: list[CandidateJudgment],
    decisions: list[ReviewDecision],
    taxonomy: Taxonomy,
) -> None:
    packet_by_id = {packet.packet_id: packet for packet in packets}
    packet_ids = set(packet_by_id)
    snapshot_ids = {snapshot.market_snapshot_id for snapshot in snapshots}
    judgment_ids = {judgment.candidate_judgment_id for judgment in judgments}
    if len(packet_ids) != len(packets):
        raise ReviewDataError("duplicate packet_id")
    if len(snapshot_ids) != len(snapshots):
        raise ReviewDataError("duplicate market_snapshot_id")
    if len(judgment_ids) != len(judgments):
        raise ReviewDataError("duplicate candidate_judgment_id")
    if len({decision.review_decision_id for decision in decisions}) != len(decisions):
        raise ReviewDataError("duplicate review_decision_id")

    for packet in packets:
        missing_snapshots = [
            snapshot_id
            for snapshot_id in packet.market_snapshot_ids
            if snapshot_id not in snapshot_ids
        ]
        if missing_snapshots:
            raise ReviewDataError(
                f"{packet.packet_id} references missing snapshots: {missing_snapshots}"
            )
        missing_judgments = [
            judgment_id
            for judgment_id in packet.candidate_judgment_ids
            if judgment_id not in judgment_ids
        ]
        if missing_judgments:
            raise ReviewDataError(
                f"{packet.packet_id} references missing judgments: {missing_judgments}"
            )

    for judgment in judgments:
        if judgment.packet_id not in packet_ids:
            raise ReviewDataError(
                f"{judgment.candidate_judgment_id} references unknown packet"
            )
    for decision in decisions:
        if decision.packet_id not in packet_ids:
            raise ReviewDataError(
                f"{decision.review_decision_id} references unknown packet"
            )
        taxonomy.validate_terms(decision.failure_taxonomy)
        if (
            decision.market_snapshot_selection == "none"
            and decision.selected_market_snapshot_id
        ):
            raise ReviewDataError(
                f"{decision.review_decision_id} cannot select a snapshot when market_snapshot_selection is none"
            )
        if (
            decision.selected_market_snapshot_id
            and decision.selected_market_snapshot_id not in snapshot_ids
        ):
            raise ReviewDataError(
                f"{decision.review_decision_id} references unknown selected snapshot"
            )
        if decision.selected_market_snapshot_id and (
            decision.selected_market_snapshot_id
            not in packet_by_id[decision.packet_id].market_snapshot_ids
        ):
            raise ReviewDataError(
                f"{decision.review_decision_id} selected snapshot is not attached to packet"
            )
        if decision.preferred_judgment_id and decision.preferred_judgment_id not in judgment_ids:
            raise ReviewDataError(
                f"{decision.review_decision_id} references unknown preferred judgment"
            )


def review_queue(
    packets: list[ReviewPacket], decisions: Iterable[ReviewDecision]
) -> list[ReviewPacket]:
    latest = latest_decisions_by_packet(decisions)
    return sorted(
        packets,
        key=lambda p: (
            1 if p.packet_id in latest else 0,
            -p.priority,
            p.source_dataset,
            p.source_case_id,
        ),
    )


def decision_rejects_case(decision: ReviewDecision) -> bool:
    return (
        decision.label_decision == "reject_case"
        or decision.recommendation == "reject"
        or decision.case_quality == "reject"
        or bool(REVIEW_NOTE_REJECT_RE.search(decision.notes))
    )


def rejected_packet_ids(decisions: Iterable[ReviewDecision]) -> set[str]:
    return {
        decision.packet_id
        for decision in decisions
        if decision_rejects_case(decision)
    }


def decision_is_reference_eligible(decision: ReviewDecision, taxonomy: Taxonomy) -> bool:
    blocked_terms = set(taxonomy.export_blocked_terms)
    explicit_market_choice = (
        decision.market_snapshot_selection == "none"
        or bool(decision.selected_market_snapshot_id)
    )
    return (
        decision.recommendation == "include_reference"
        and decision.case_quality == "usable"
        and decision.confidence == "high"
        and decision.label_decision in {"keep", "change"}
        and explicit_market_choice
        and not blocked_terms.intersection(decision.failure_taxonomy)
    )


def export_clean_golden_samples(
    packets: list[ReviewPacket],
    snapshots: list[MarketSnapshot],
    decisions: list[ReviewDecision],
    taxonomy: Taxonomy,
) -> list[dict[str, Any]]:
    packet_by_id = {packet.packet_id: packet for packet in packets}
    snapshot_by_id = {snapshot.market_snapshot_id: snapshot for snapshot in snapshots}
    rejected_packets = rejected_packet_ids(decisions)
    rows: list[dict[str, Any]] = []
    for packet_id, decision in latest_decisions_by_packet(decisions).items():
        if packet_id in rejected_packets:
            continue
        packet = packet_by_id.get(packet_id)
        if packet is None or packet.source_scope == "synthetic_stress":
            continue
        if not decision_is_reference_eligible(decision, taxonomy):
            continue
        if not _packet_has_reviewable_source(packet):
            continue
        if not proposed_thesis_text(packet):
            continue
        candidate_snapshots = [
            snapshot_by_id[snapshot_id]
            for snapshot_id in packet.market_snapshot_ids
            if snapshot_id in snapshot_by_id
            and _market_snapshot_is_clean_golden_evidence(snapshot_by_id[snapshot_id])
        ]
        selected_snapshot = (
            snapshot_by_id.get(decision.selected_market_snapshot_id or "")
            if decision.selected_market_snapshot_id
            else None
        )
        if not _decision_market_selection_is_clean(
            decision=decision,
            selected_snapshot=selected_snapshot,
            candidate_snapshots=candidate_snapshots,
        ):
            continue
        rows.append(
            {
                "schema_version": "clean_golden_sample_v1",
                "dataset_version": CLEAN_GOLDEN_DATASET_VERSION,
                "criteria_version": CLEAN_GOLDEN_CRITERIA_VERSION,
                "clean_sample_id": _clean_sample_id(packet, decision),
                "packet_id": packet.packet_id,
                "review_decision_id": decision.review_decision_id,
                "source_dataset": packet.source_dataset,
                "source_case_id": packet.source_case_id,
                "source_scope": packet.source_scope,
                "source_url": packet.source_provenance.get("source_url"),
                "source_name": packet.source_provenance.get("source_name"),
                "source_text": packet.source_text,
                "source_provenance": packet.source_provenance,
                "normalized_thesis": proposed_thesis_text(packet),
                "normalized_claim": packet.normalized_claim,
                "fit_class": decision.fit_class,
                "failure_taxonomy": decision.failure_taxonomy,
                "false_strong_risk": decision.false_strong_risk,
                "market_snapshot_selection": decision.market_snapshot_selection,
                "selected_market_snapshot_id": (
                    selected_snapshot.market_snapshot_id if selected_snapshot else None
                ),
                "selected_market_id": (
                    selected_snapshot.market_id if selected_snapshot else None
                ),
                "candidate_market_snapshot_ids": [
                    snapshot.market_snapshot_id for snapshot in candidate_snapshots
                ],
                "candidate_market_ids": [
                    snapshot.market_id for snapshot in candidate_snapshots
                ],
                "selected_market_snapshot": (
                    selected_snapshot.model_dump(mode="json")
                    if selected_snapshot
                    else None
                ),
                "candidate_market_snapshots": [
                    snapshot.model_dump(mode="json")
                    for snapshot in candidate_snapshots
                ],
                "review": {
                    "reviewer_id": decision.reviewer_id,
                    "reviewed_at_utc": decision.reviewed_at_utc,
                    "label_decision": decision.label_decision,
                    "case_quality": decision.case_quality,
                    "confidence": decision.confidence,
                    "recommendation": decision.recommendation,
                    "notes": decision.notes,
                },
                "promotion_status": "promoted",
                "promotion_source": "latest_human_review",
            }
        )
    return sorted(rows, key=lambda row: row["clean_sample_id"])


def export_clean_golden_market_snapshots(
    clean_samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for sample in clean_samples:
        for snapshot in sample["candidate_market_snapshots"]:
            snapshots[snapshot["market_snapshot_id"]] = {
                **snapshot,
                "schema_version": "clean_market_snapshot_v1",
                "dataset_version": CLEAN_GOLDEN_DATASET_VERSION,
            }
    return [snapshots[key] for key in sorted(snapshots)]


def export_reviewed_fit_testing_samples(
    packets: list[ReviewPacket],
    snapshots: list[MarketSnapshot],
    decisions: list[ReviewDecision],
    taxonomy: Taxonomy,
) -> list[dict[str, Any]]:
    """Experiment dataset: clean primary rows plus reviewed legacy regressions.

    Legacy rows are useful for broad regression tests, but they are explicitly
    tiered below clean goldens because their source pointers are fixture-era
    placeholders rather than real-world source URLs.
    """
    clean_samples = export_clean_golden_samples(
        packets=packets,
        snapshots=snapshots,
        decisions=decisions,
        taxonomy=taxonomy,
    )
    rows = [_testing_sample_from_clean(sample) for sample in clean_samples]
    rows.extend(
        export_legacy_golden_testing_samples(
            packets=packets,
            snapshots=snapshots,
            decisions=decisions,
            taxonomy=taxonomy,
        )
    )
    return sorted(
        rows,
        key=lambda row: (
            row["evaluation_tier"],
            row["source_dataset"],
            row["source_case_id"],
            row["test_sample_id"],
        ),
    )


def export_legacy_golden_testing_samples(
    packets: list[ReviewPacket],
    snapshots: list[MarketSnapshot],
    decisions: list[ReviewDecision],
    taxonomy: Taxonomy,
) -> list[dict[str, Any]]:
    packet_by_id = {packet.packet_id: packet for packet in packets}
    snapshot_by_id = {snapshot.market_snapshot_id: snapshot for snapshot in snapshots}
    rejected_packets = rejected_packet_ids(decisions)
    rows: list[dict[str, Any]] = []
    for packet_id, decision in latest_decisions_by_packet(decisions).items():
        if packet_id in rejected_packets:
            continue
        packet = packet_by_id.get(packet_id)
        if packet is None or packet.source_dataset != "fitcheck_legacy_goldens":
            continue
        if not decision_is_reference_eligible(decision, taxonomy):
            continue
        if not proposed_thesis_text(packet):
            continue
        candidate_snapshots = [
            snapshot_by_id[snapshot_id]
            for snapshot_id in packet.market_snapshot_ids
            if snapshot_id in snapshot_by_id
        ]
        selected_snapshot = (
            snapshot_by_id.get(decision.selected_market_snapshot_id or "")
            if decision.selected_market_snapshot_id
            else None
        )
        rows.append(
            _testing_sample_from_packet(
                packet=packet,
                decision=decision,
                selected_snapshot=selected_snapshot,
                candidate_snapshots=candidate_snapshots,
                evaluation_tier="legacy_regression",
                qualification_warnings=_legacy_testing_warnings(
                    packet=packet,
                    decision=decision,
                    selected_snapshot=selected_snapshot,
                    candidate_snapshots=candidate_snapshots,
                ),
            )
        )
    return sorted(rows, key=lambda row: row["test_sample_id"])


def export_reviewed_fit_testing_market_snapshots(
    testing_samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for sample in testing_samples:
        for snapshot in sample["candidate_market_snapshots"]:
            snapshots[snapshot["market_snapshot_id"]] = {
                **snapshot,
                "schema_version": "reviewed_fit_testing_market_snapshot_v1",
                "dataset_version": REVIEWED_FIT_TESTING_DATASET_VERSION,
            }
    return [snapshots[key] for key in sorted(snapshots)]


def _testing_sample_from_clean(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "reviewed_fit_test_sample_v1",
        "dataset_version": REVIEWED_FIT_TESTING_DATASET_VERSION,
        "evaluation_tier": "clean_primary",
        "test_sample_id": _testing_sample_id(
            "clean_primary", sample["packet_id"], sample["review_decision_id"]
        ),
        "source_dataset": sample["source_dataset"],
        "source_case_id": sample["source_case_id"],
        "source_scope": sample["source_scope"],
        "source_url": sample["source_url"],
        "source_name": sample["source_name"],
        "source_text": sample["source_text"],
        "source_provenance": sample["source_provenance"],
        "normalized_thesis": sample["normalized_thesis"],
        "normalized_claim": sample["normalized_claim"],
        "fit_class": sample["fit_class"],
        "failure_taxonomy": sample["failure_taxonomy"],
        "false_strong_risk": sample["false_strong_risk"],
        "market_snapshot_selection": sample["market_snapshot_selection"],
        "selected_market_snapshot_id": sample["selected_market_snapshot_id"],
        "selected_market_id": sample["selected_market_id"],
        "candidate_market_snapshot_ids": sample["candidate_market_snapshot_ids"],
        "candidate_market_ids": sample["candidate_market_ids"],
        "selected_market_snapshot": sample["selected_market_snapshot"],
        "candidate_market_snapshots": sample["candidate_market_snapshots"],
        "review": sample["review"],
        "packet_id": sample["packet_id"],
        "review_decision_id": sample["review_decision_id"],
        "clean_golden_sample_id": sample["clean_sample_id"],
        "qualification_warnings": [],
    }


def _testing_sample_from_packet(
    *,
    packet: ReviewPacket,
    decision: ReviewDecision,
    selected_snapshot: MarketSnapshot | None,
    candidate_snapshots: list[MarketSnapshot],
    evaluation_tier: str,
    qualification_warnings: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "reviewed_fit_test_sample_v1",
        "dataset_version": REVIEWED_FIT_TESTING_DATASET_VERSION,
        "evaluation_tier": evaluation_tier,
        "test_sample_id": _testing_sample_id(
            evaluation_tier, packet.packet_id, decision.review_decision_id
        ),
        "source_dataset": packet.source_dataset,
        "source_case_id": packet.source_case_id,
        "source_scope": packet.source_scope,
        "source_url": packet.source_provenance.get("source_url"),
        "source_name": packet.source_provenance.get("source_name"),
        "source_text": packet.source_text,
        "source_provenance": packet.source_provenance,
        "normalized_thesis": proposed_thesis_text(packet),
        "normalized_claim": packet.normalized_claim,
        "fit_class": decision.fit_class,
        "failure_taxonomy": decision.failure_taxonomy,
        "false_strong_risk": decision.false_strong_risk,
        "market_snapshot_selection": decision.market_snapshot_selection,
        "selected_market_snapshot_id": (
            selected_snapshot.market_snapshot_id if selected_snapshot else None
        ),
        "selected_market_id": selected_snapshot.market_id if selected_snapshot else None,
        "candidate_market_snapshot_ids": [
            snapshot.market_snapshot_id for snapshot in candidate_snapshots
        ],
        "candidate_market_ids": [snapshot.market_id for snapshot in candidate_snapshots],
        "selected_market_snapshot": (
            selected_snapshot.model_dump(mode="json") if selected_snapshot else None
        ),
        "candidate_market_snapshots": [
            snapshot.model_dump(mode="json") for snapshot in candidate_snapshots
        ],
        "review": {
            "reviewer_id": decision.reviewer_id,
            "reviewed_at_utc": decision.reviewed_at_utc,
            "label_decision": decision.label_decision,
            "case_quality": decision.case_quality,
            "confidence": decision.confidence,
            "recommendation": decision.recommendation,
            "notes": decision.notes,
        },
        "packet_id": packet.packet_id,
        "review_decision_id": decision.review_decision_id,
        "clean_golden_sample_id": None,
        "qualification_warnings": qualification_warnings,
    }


def _legacy_testing_warnings(
    *,
    packet: ReviewPacket,
    decision: ReviewDecision,
    selected_snapshot: MarketSnapshot | None,
    candidate_snapshots: list[MarketSnapshot],
) -> list[str]:
    warnings = ["legacy_regression_not_clean_golden"]
    if not _packet_has_reviewable_source(packet):
        warnings.append("source_unavailable")
    if not candidate_snapshots:
        warnings.append("missing_candidate_market_snapshots")
    if decision.market_snapshot_selection == "selected" and selected_snapshot is None:
        warnings.append("missing_selected_market_snapshot")
    return warnings


def _testing_sample_id(tier: str, packet_id: str, review_decision_id: str) -> str:
    digest = hashlib.sha256(
        canonical_json(
            {
                "tier": tier,
                "packet_id": packet_id,
                "review_decision_id": review_decision_id,
                "dataset_version": REVIEWED_FIT_TESTING_DATASET_VERSION,
            }
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"rfts_{digest}"


def _clean_sample_id(packet: ReviewPacket, decision: ReviewDecision) -> str:
    digest = hashlib.sha256(
        canonical_json(
            {
                "packet_id": packet.packet_id,
                "review_decision_id": decision.review_decision_id,
                "criteria_version": CLEAN_GOLDEN_CRITERIA_VERSION,
            }
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"cg_{digest}"


def _packet_has_reviewable_source(packet: ReviewPacket) -> bool:
    source_url = str(packet.source_provenance.get("source_url") or "").strip()
    if not source_url:
        return False
    return "source-unavailable" not in source_url.lower()


def _market_snapshot_is_clean_golden_evidence(snapshot: MarketSnapshot) -> bool:
    return (
        bool(snapshot.market_id.strip())
        and bool(snapshot.resolution_rules.strip())
        and bool(str(snapshot.url or "").strip())
        and snapshot.snapshot_kind != "embedded_stress"
        and snapshot.retrieval_provider != "mfta_stress_fixture"
    )


def _decision_market_selection_is_clean(
    *,
    decision: ReviewDecision,
    selected_snapshot: MarketSnapshot | None,
    candidate_snapshots: list[MarketSnapshot],
) -> bool:
    if not candidate_snapshots:
        return False
    if decision.fit_class == "no_clean_expression":
        return (
            decision.market_snapshot_selection == "none"
            and selected_snapshot is None
        )
    return (
        decision.market_snapshot_selection == "selected"
        and selected_snapshot is not None
        and _market_snapshot_is_clean_golden_evidence(selected_snapshot)
    )


def export_reference_labels(
    packets: list[ReviewPacket],
    decisions: list[ReviewDecision],
    taxonomy: Taxonomy,
) -> list[dict[str, Any]]:
    packet_by_id = {packet.packet_id: packet for packet in packets}
    rejected_packets = rejected_packet_ids(decisions)
    rows: list[dict[str, Any]] = []
    for packet_id, decision in latest_decisions_by_packet(decisions).items():
        if packet_id in rejected_packets:
            continue
        packet = packet_by_id.get(packet_id)
        if packet is None or packet.source_scope == "synthetic_stress":
            continue
        if not decision_is_reference_eligible(decision, taxonomy):
            continue
        rows.append(
            {
                "schema_version": "reference_label_v0",
                "packet_id": packet.packet_id,
                "review_decision_id": decision.review_decision_id,
                "source_dataset": packet.source_dataset,
                "source_case_id": packet.source_case_id,
                "source_scope": packet.source_scope,
                "fit_class": decision.fit_class,
                "market_snapshot_selection": decision.market_snapshot_selection,
                "selected_market_snapshot_id": decision.selected_market_snapshot_id,
                "false_strong_risk": decision.false_strong_risk,
                "reviewed_at_utc": decision.reviewed_at_utc,
            }
        )
    return sorted(rows, key=lambda row: row["packet_id"])


def export_stress_suite(
    packets: list[ReviewPacket],
    decisions: list[ReviewDecision],
) -> list[dict[str, Any]]:
    packet_by_id = {packet.packet_id: packet for packet in packets}
    rejected_packets = rejected_packet_ids(decisions)
    rows: list[dict[str, Any]] = []
    for packet_id, decision in latest_decisions_by_packet(decisions).items():
        if packet_id in rejected_packets:
            continue
        packet = packet_by_id.get(packet_id)
        if packet is None or packet.source_scope != "synthetic_stress":
            continue
        if (
            decision.recommendation != "include_stress"
            or decision.case_quality != "usable"
            or decision.label_decision not in {"keep", "change"}
            or decision.confidence == "low"
            or (
                decision.market_snapshot_selection != "none"
                and not decision.selected_market_snapshot_id
            )
        ):
            continue
        rows.append(
            {
                "schema_version": "stress_label_v0",
                "packet_id": packet.packet_id,
                "review_decision_id": decision.review_decision_id,
                "source_dataset": packet.source_dataset,
                "source_case_id": packet.source_case_id,
                "fit_class": decision.fit_class,
                "market_snapshot_selection": decision.market_snapshot_selection,
                "selected_market_snapshot_id": decision.selected_market_snapshot_id,
            }
        )
    return sorted(rows, key=lambda row: row["packet_id"])


def export_preference_pairs(
    packets: list[ReviewPacket],
    judgments: list[CandidateJudgment],
    decisions: list[ReviewDecision],
) -> list[dict[str, Any]]:
    packet_ids = {packet.packet_id for packet in packets}
    judgments_by_packet: dict[str, list[CandidateJudgment]] = {}
    for judgment in judgments:
        judgments_by_packet.setdefault(judgment.packet_id, []).append(judgment)
    rows: list[dict[str, Any]] = []
    for decision in latest_decisions_by_packet(decisions).values():
        if (
            decision.packet_id not in packet_ids
            or not decision.preferred_judgment_id
            or decision.preference_strength not in {"strong", "weak"}
        ):
            continue
        packet_judgments = judgments_by_packet.get(decision.packet_id, [])
        preferred = next(
            (
                judgment
                for judgment in packet_judgments
                if judgment.candidate_judgment_id == decision.preferred_judgment_id
            ),
            None,
        )
        if preferred is None:
            continue
        for rejected in packet_judgments:
            if rejected.candidate_judgment_id == preferred.candidate_judgment_id:
                continue
            rows.append(
                {
                    "schema_version": "preference_pair_v0",
                    "packet_id": decision.packet_id,
                    "review_decision_id": decision.review_decision_id,
                    "preferred_judgment_id": preferred.candidate_judgment_id,
                    "rejected_judgment_id": rejected.candidate_judgment_id,
                    "preference_strength": decision.preference_strength,
                    "false_strong_risk": decision.false_strong_risk,
                }
            )
    return sorted(
        rows,
        key=lambda row: (
            row["packet_id"],
            row["preferred_judgment_id"],
            row["rejected_judgment_id"],
        ),
    )


def make_review_decision(
    payload: ReviewDecisionInput,
    *,
    now: datetime | None = None,
) -> ReviewDecision:
    reviewed_at = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
    safe_reviewer = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in payload.reviewer_id
    )
    digest = hashlib.sha256(
        canonical_json(payload.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()[:12]
    decision_id = f"rd_{payload.packet_id}_{safe_reviewer}_{digest}"
    return ReviewDecision(
        review_decision_id=decision_id,
        reviewed_at_utc=reviewed_at,
        **payload.model_dump(mode="json"),
    )


def build_governance_packet(row: dict[str, Any]) -> ReviewPacket:
    source_case_id = str(row.get("governance_id") or row.get("case_id"))
    source_fields = {
        "governance_id": row.get("governance_id"),
        "case_id": row.get("case_id"),
        "source_text": row.get("source_text", ""),
        "expected_fit_class": row.get("expected_fit_class") or row.get("fit_class"),
        "expected_best_market_id": row.get("expected_best_market_id"),
        "acceptable_market_ids": row.get("acceptable_market_ids", []),
        "rejected_market_ids": row.get("rejected_market_ids", []),
        "truth_scope": row.get("truth_scope"),
        "review_status": row.get("review_status"),
    }
    return ReviewPacket(
        packet_id=f"pkt_mfta_governance_50_{source_case_id}",
        source_dataset="mfta_governance_50",
        source_case_id=source_case_id,
        source_scope="real_world_reference_candidate",
        source_row_hash=stable_hash(source_fields),
        source_text=str(row.get("source_text", "")),
        source_provenance=dict(row.get("source_provenance") or {}),
        normalized_claim=dict(row.get("normalized_claim") or {}),
        reference_candidate={
            "fit_class": row.get("expected_fit_class") or row.get("fit_class"),
            "best_market_id": row.get("expected_best_market_id"),
            "acceptable_market_ids": row.get("acceptable_market_ids", []),
            "rejected_market_ids": row.get("rejected_market_ids", []),
            "review_status_from_source": row.get("review_status"),
            "truth_scope_from_source": row.get("truth_scope"),
            "expected_behavior": row.get("expected_behavior"),
            "failure_modes": row.get("failure_modes", []),
        },
        market_snapshot_ids=[],
        candidate_judgment_ids=[],
        priority=int(row.get("curation_priority") or 0),
        review_state="unreviewed",
    )


def build_stress_packet(row: dict[str, Any]) -> tuple[ReviewPacket, MarketSnapshot]:
    market = dict(row.get("market") or {})
    source_case_id = str(row["case_id"])
    snapshot = MarketSnapshot(
        market_snapshot_id=f"ms_mfta_stress_40_{source_case_id}_{market.get('market_id')}",
        market_id=str(market.get("market_id")),
        venue=str(market.get("venue") or "SyntheticStress"),
        url=None,
        title=str(market.get("title") or ""),
        description=str(market.get("description") or ""),
        resolution_rules=str(market.get("resolution_rules") or ""),
        outcomes=list(market.get("outcomes") or []),
        close_date=market.get("close_date"),
        status="synthetic",
        captured_at_utc="2026-06-14T00:00:00Z",
        snapshot_kind="embedded_stress",
        retrieval_provider="mfta_stress_fixture",
        retrieval_id=source_case_id,
        retrieval_query=row.get("mismatch_family"),
        polydata_cutoff_at_utc=None,
        market_role="stress_embedded",
    )
    source_fields = {
        "case_id": row.get("case_id"),
        "thesis": row.get("thesis"),
        "expected_fit_class": row.get("expected_fit_class"),
        "market": market,
        "mismatch_family": row.get("mismatch_family"),
    }
    packet = ReviewPacket(
        packet_id=f"pkt_mfta_stress_40_{source_case_id}",
        source_dataset="mfta_stress_40",
        source_case_id=source_case_id,
        source_scope="synthetic_stress",
        source_row_hash=stable_hash(source_fields),
        source_text=str(row.get("thesis") or ""),
        source_provenance={
            "truth_scope": row.get("truth_scope"),
            "expected_label_source": row.get("expected_label_source"),
            "trap_description": row.get("trap_description"),
        },
        normalized_claim={"claim_text": row.get("thesis")},
        reference_candidate={
            "fit_class": row.get("expected_fit_class"),
            "best_market_id": market.get("market_id"),
            "acceptable_market_ids": [],
            "rejected_market_ids": [],
            "truth_scope_from_source": row.get("truth_scope"),
            "failure_modes": [row.get("mismatch_family")],
        },
        market_snapshot_ids=[snapshot.market_snapshot_id],
        candidate_judgment_ids=[],
        priority=25,
        review_state="unreviewed",
    )
    return packet, snapshot


def build_source_candidate_packet(
    example: dict[str, Any],
    expected: dict[str, Any] | None,
    *,
    pack_name: str,
) -> ReviewPacket:
    expected = expected or {}
    expected_fit = dict(expected.get("expected_fit") or {})
    source_case_id = str(example.get("example_id"))
    source_fields = {
        "example_id": example.get("example_id"),
        "source_text": example.get("source_text"),
        "source_provenance": example.get("source_provenance", {}),
        "expected_fit": expected_fit,
    }
    return ReviewPacket(
        packet_id=f"pkt_{pack_name}_{source_case_id}",
        source_dataset=pack_name,
        source_case_id=source_case_id,
        source_scope="source_text_and_provenance_only",
        source_row_hash=stable_hash(source_fields),
        source_text=str(example.get("source_text") or ""),
        source_provenance=dict(example.get("source_provenance") or {}),
        normalized_claim=dict(expected.get("expected_thesis") or {}),
        reference_candidate={
            "fit_class": expected_fit.get("semantic_fit_class"),
            "best_market_id": expected_fit.get("best_market_id"),
            "acceptable_market_ids": expected_fit.get("acceptable_market_ids", []),
            "rejected_market_ids": expected_fit.get("rejected_market_ids", []),
            "truth_scope_from_source": "source_text_and_provenance_only",
            "minimum_expected_behavior": expected_fit.get("minimum_expected_behavior"),
            "case_tags": expected_fit.get("case_tags", []),
        },
        market_snapshot_ids=[],
        candidate_judgment_ids=[],
        priority=10,
        review_state="unreviewed",
    )


def parse_model_rows(model: type[BaseModel], rows: Iterable[dict[str, Any]]) -> list[BaseModel]:
    parsed: list[BaseModel] = []
    for row in rows:
        try:
            parsed.append(model.model_validate(row))
        except ValidationError as exc:
            raise ReviewDataError(str(exc)) from exc
    return parsed
