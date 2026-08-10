"""Local FastAPI review console for the reviewed reference dataset."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from el.review.reference_dataset import (
    CandidateJudgment,
    MarketSnapshotEditInput,
    MarketSnapshot,
    ReviewDataError,
    ReviewDecision,
    ReviewDecisionInput,
    ReviewPacket,
    Taxonomy,
    append_jsonl,
    export_clean_golden_market_snapshots,
    export_clean_golden_samples,
    export_preference_pairs,
    export_reference_labels,
    export_stress_suite,
    file_hash,
    is_decision_ready_packet,
    latest_decisions_by_packet,
    load_candidate_judgments,
    load_decisions,
    load_packets,
    load_snapshots,
    load_taxonomy,
    make_review_decision,
    proposed_thesis_text,
    rejected_packet_ids,
    review_readiness_warnings,
    review_queue,
    stable_hash,
    validate_review_input_completeness,
    validate_references,
    write_jsonl,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_ROOT = ROOT / "data" / "review"
STATIC_HTML = Path(__file__).resolve().parent / "static" / "review_console.html"
MARKET_REQUIRED_FIT_CLASSES = {"direct", "indirect", "weak_proxy"}
MANUAL_CORRECTION_RECHECK_MODE = "manual_correction_requested_v1"
DECISION_QUALITY_CORRECTION_RECHECK_MODE = "decision_quality_correction_v1"


class ReviewRepository:
    def __init__(self, review_root: Path = DEFAULT_REVIEW_ROOT):
        self.review_root = review_root
        self.packets_path = review_root / "review_packets_v0.jsonl"
        self.decisions_path = review_root / "review_decisions_v0.jsonl"
        self.taxonomy_path = review_root / "review_taxonomy_v0.json"
        self.snapshots_path = review_root / "market_snapshots_v0.jsonl"
        self.judgments_path = review_root / "candidate_judgments_v0.jsonl"

    def taxonomy(self) -> Taxonomy:
        return load_taxonomy(self.taxonomy_path)

    def packets(self) -> list[ReviewPacket]:
        return load_packets(self.packets_path)

    def snapshots(self):
        return load_snapshots(self.snapshots_path)

    def judgments(self) -> list[CandidateJudgment]:
        return load_candidate_judgments(self.judgments_path)

    def decisions(self) -> list[ReviewDecision]:
        return load_decisions(self.decisions_path, self.taxonomy())

    def validate(self) -> None:
        validate_references(
            packets=self.packets(),
            snapshots=self.snapshots(),
            judgments=self.judgments(),
            decisions=self.decisions(),
            taxonomy=self.taxonomy(),
        )

    def list_packets(
        self,
        *,
        include_reviewed: bool = False,
        include_incomplete: bool = False,
        include_duplicates: bool = False,
    ) -> list[dict[str, Any]]:
        packets = self.packets()
        decisions = self.decisions()
        latest = latest_decisions_by_packet(decisions)
        rejected_packets = rejected_packet_ids(decisions)
        snapshot_by_id = {
            snapshot.market_snapshot_id: snapshot for snapshot in self.snapshots()
        }
        duplicate_info = _duplicate_payload_index(packets, snapshot_by_id)
        rejected_fingerprints = _rejected_payload_fingerprints(
            rejected_packets, duplicate_info
        )
        queue = review_queue(packets, latest.values())
        if not include_reviewed:
            queue = [packet for packet in queue if packet.packet_id not in latest]
            queue = [
                packet
                for packet in queue
                if not _has_rejected_parent(packet, rejected_packets)
            ]
            queue = [
                packet
                for packet in queue
                if duplicate_info[packet.packet_id]["fingerprint"]
                not in rejected_fingerprints
            ]
        if not include_incomplete:
            queue = [packet for packet in queue if is_decision_ready_packet(packet)]
        if not include_duplicates and not include_reviewed:
            queue = _collapse_duplicate_payloads(
                queue=queue,
                all_packets=packets,
                latest=latest,
                duplicate_info=duplicate_info,
            )
        return [
            {
                "packet_id": packet.packet_id,
                "source_dataset": packet.source_dataset,
                "source_case_id": packet.source_case_id,
                "source_scope": packet.source_scope,
                "priority": packet.priority,
                "review_state": (
                    latest[packet.packet_id].recommendation
                    if packet.packet_id in latest
                    else packet.review_state
                ),
                "fit_class": packet.reference_candidate.get("fit_class"),
                "decision_ready": is_decision_ready_packet(packet),
                "readiness_warnings": review_readiness_warnings(packet),
                "payload_fingerprint": duplicate_info[packet.packet_id]["fingerprint"],
                "duplicate_packet_ids": duplicate_info[packet.packet_id]["packet_ids"],
                "duplicate_count": len(duplicate_info[packet.packet_id]["packet_ids"]),
                "parent_rejected": _has_rejected_parent(packet, rejected_packets),
                "payload_rejected": (
                    duplicate_info[packet.packet_id]["fingerprint"]
                    in rejected_fingerprints
                ),
            }
            for packet in queue
        ]

    def list_corrections(self) -> list[dict[str, Any]]:
        packets = self.packets()
        decisions = self.decisions()
        latest = latest_decisions_by_packet(decisions)
        rejected_packets = rejected_packet_ids(decisions)
        snapshot_by_id = {
            snapshot.market_snapshot_id: snapshot for snapshot in self.snapshots()
        }
        duplicate_info = _duplicate_payload_index(packets, snapshot_by_id)
        rejected_fingerprints = _rejected_payload_fingerprints(
            rejected_packets, duplicate_info
        )
        rows: list[dict[str, Any]] = []
        for packet in packets:
            if packet.packet_id in rejected_packets:
                continue
            if _has_rejected_parent(packet, rejected_packets):
                continue
            if (
                duplicate_info[packet.packet_id]["fingerprint"]
                in rejected_fingerprints
            ):
                continue
            decision = latest.get(packet.packet_id)
            if decision is None:
                continue
            audit_flags = _decision_quality_flags(decision)
            if not audit_flags:
                continue
            rows.append(
                {
                    "packet_id": packet.packet_id,
                    "source_dataset": packet.source_dataset,
                    "source_case_id": packet.source_case_id,
                    "source_scope": packet.source_scope,
                    "priority": packet.priority,
                    "review_state": decision.recommendation,
                    "fit_class": decision.fit_class,
                    "source_fit_class": packet.reference_candidate.get("fit_class"),
                    "latest_decision_id": decision.review_decision_id,
                    "reviewed_at_utc": decision.reviewed_at_utc,
                    "selected_market_snapshot_id": decision.selected_market_snapshot_id,
                    "recommendation": decision.recommendation,
                    "case_quality": decision.case_quality,
                    "label_decision": decision.label_decision,
                    "recheck_status": decision.recheck_status,
                    "rechecked_at_utc": decision.rechecked_at_utc,
                    "recheck_mode": decision.recheck_mode,
                    "audit_flags": audit_flags,
                    "audit_severity": _decision_quality_severity(audit_flags),
                    "payload_fingerprint": duplicate_info[packet.packet_id]["fingerprint"],
                    "duplicate_packet_ids": duplicate_info[packet.packet_id]["packet_ids"],
                    "duplicate_count": len(duplicate_info[packet.packet_id]["packet_ids"]),
                }
            )
        return sorted(
            rows,
            key=lambda row: (
                -row["audit_severity"],
                -row["priority"],
                row["source_dataset"],
                row["source_case_id"],
            ),
        )

    def packet_detail(self, packet_id: str) -> dict[str, Any]:
        packets = {packet.packet_id: packet for packet in self.packets()}
        packet = packets.get(packet_id)
        if packet is None:
            raise ReviewDataError(f"unknown packet_id: {packet_id}")
        snapshot_by_id = {
            snapshot.market_snapshot_id: snapshot for snapshot in self.snapshots()
        }
        duplicate_info = _duplicate_payload_index(list(packets.values()), snapshot_by_id)
        judgment_by_id = {
            judgment.candidate_judgment_id: judgment for judgment in self.judgments()
        }
        missing_snapshot_ids = [
            snapshot_id
            for snapshot_id in packet.market_snapshot_ids
            if snapshot_id not in snapshot_by_id
        ]
        missing_judgment_ids = [
            judgment_id
            for judgment_id in packet.candidate_judgment_ids
            if judgment_id not in judgment_by_id
        ]
        latest_by_packet = latest_decisions_by_packet(self.decisions())
        latest = latest_by_packet.get(packet_id)
        market_snapshots = _snapshots_for_review_display(packet, snapshot_by_id)
        duplicate_packet_ids = duplicate_info[packet.packet_id]["packet_ids"]
        pending_packets = [
            candidate
            for candidate in packets.values()
            if candidate.packet_id not in latest_by_packet
            and is_decision_ready_packet(candidate)
        ]
        thesis_peer_packet_ids = _thesis_peer_packet_ids(packet, pending_packets)
        return {
            "packet": packet.model_dump(mode="json"),
            "review_focus": _review_focus(
                packet,
                market_snapshots=market_snapshots,
                duplicate_packet_ids=duplicate_packet_ids,
                thesis_peer_packet_ids=thesis_peer_packet_ids,
            ),
            "market_snapshots": [
                snapshot.model_dump(mode="json") for snapshot in market_snapshots
            ],
            "candidate_judgments": [
                judgment_by_id[judgment_id].model_dump(mode="json")
                for judgment_id in packet.candidate_judgment_ids
                if judgment_id in judgment_by_id
            ],
            "latest_decision": latest.model_dump(mode="json") if latest else None,
            "missing_snapshot_ids": missing_snapshot_ids,
            "missing_judgment_ids": missing_judgment_ids,
            "decision_ready": is_decision_ready_packet(packet) and not missing_snapshot_ids,
            "readiness_warnings": review_readiness_warnings(packet) + missing_snapshot_ids,
            "payload_fingerprint": duplicate_info[packet.packet_id]["fingerprint"],
            "duplicate_packet_ids": duplicate_packet_ids,
            "status": "unavailable" if missing_snapshot_ids else "ok",
        }

    def append_decision(self, payload: ReviewDecisionInput) -> ReviewDecision:
        payload, _ = self._validated_decision_payload(payload)
        decision = make_review_decision(payload)
        append_jsonl(self.decisions_path, decision)
        return decision

    def replace_decision(
        self, review_decision_id: str, payload: ReviewDecisionInput
    ) -> ReviewDecision:
        packets = self.packets()
        snapshots = self.snapshots()
        judgments = self.judgments()
        taxonomy = self.taxonomy()
        decisions = self.decisions()
        existing_index = next(
            (
                index
                for index, decision in enumerate(decisions)
                if decision.review_decision_id == review_decision_id
            ),
            None,
        )
        if existing_index is None:
            raise ReviewDataError(f"unknown review_decision_id: {review_decision_id}")
        existing = decisions[existing_index]
        latest = latest_decisions_by_packet(decisions)
        if latest.get(existing.packet_id) != existing:
            raise ReviewDataError(
                "only the latest review decision for a packet can be corrected"
            )
        if payload.packet_id != existing.packet_id:
            raise ReviewDataError(
                "correction payload packet_id must match the existing decision"
            )
        payload, _ = self._validated_decision_payload(
            payload,
            packets=packets,
            judgments=judgments,
            taxonomy=taxonomy,
        )
        replacement = ReviewDecision(
            review_decision_id=existing.review_decision_id,
            reviewed_at_utc=existing.reviewed_at_utc,
            recheck_status="cleared",
            rechecked_at_utc=datetime.now(UTC).isoformat(),
            recheck_mode=DECISION_QUALITY_CORRECTION_RECHECK_MODE,
            **payload.model_dump(mode="json"),
        )
        updated_decisions = list(decisions)
        updated_decisions[existing_index] = replacement
        validate_references(
            packets=packets,
            snapshots=snapshots,
            judgments=judgments,
            decisions=updated_decisions,
            taxonomy=taxonomy,
        )
        write_jsonl(self.decisions_path, updated_decisions)
        return replacement

    def _validated_decision_payload(
        self,
        payload: ReviewDecisionInput,
        *,
        packets: list[ReviewPacket] | None = None,
        judgments: list[CandidateJudgment] | None = None,
        taxonomy: Taxonomy | None = None,
    ) -> tuple[ReviewDecisionInput, ReviewPacket]:
        packets_by_id = {
            packet.packet_id: packet for packet in (packets or self.packets())
        }
        packet = packets_by_id.get(payload.packet_id)
        if packet is None:
            raise ReviewDataError(f"unknown packet_id: {payload.packet_id}")
        validate_review_input_completeness(payload)
        if not packet.market_snapshot_ids:
            raise ReviewDataError(
                "cannot submit review without frozen market snapshots"
            )
        if (
            payload.market_snapshot_selection == "none"
            and payload.selected_market_snapshot_id
        ):
            raise ReviewDataError(
                "selected_market_snapshot_id must be empty when market_snapshot_selection is none"
            )
        if (
            payload.market_snapshot_selection == "selected"
            and not payload.selected_market_snapshot_id
        ):
            raise ReviewDataError("selected_market_snapshot_id is required")
        if (
            payload.selected_market_snapshot_id
            and payload.selected_market_snapshot_id not in packet.market_snapshot_ids
        ):
            raise ReviewDataError(
                "selected_market_snapshot_id must be one of this packet's market_snapshot_ids"
            )
        if payload.fit_class in MARKET_REQUIRED_FIT_CLASSES:
            if payload.market_snapshot_selection != "selected":
                raise ReviewDataError(
                    "direct, indirect, and weak_proxy reviews require one selected market snapshot"
                )
        if payload.fit_class == "no_clean_expression":
            if payload.market_snapshot_selection != "none":
                raise ReviewDataError(
                    "no_clean_expression reviews require market_snapshot_selection=none"
                )
        taxonomy = taxonomy or self.taxonomy()
        normalized_taxonomy = taxonomy.normalize_terms(payload.failure_taxonomy)
        taxonomy.validate_terms(normalized_taxonomy)
        payload = payload.model_copy(
            update={"failure_taxonomy": normalized_taxonomy}
        )
        judgment_ids = {
            judgment.candidate_judgment_id for judgment in (judgments or self.judgments())
        }
        if payload.preferred_judgment_id and payload.preferred_judgment_id not in judgment_ids:
            raise ReviewDataError(
                f"unknown preferred_judgment_id: {payload.preferred_judgment_id}"
            )
        return payload, packet

    def save_market_snapshot_edit(
        self, packet_id: str, payload: MarketSnapshotEditInput
    ) -> tuple[MarketSnapshot, str | None]:
        packets = self.packets()
        snapshots = self.snapshots()
        judgments = self.judgments()
        decisions = self.decisions()
        taxonomy = self.taxonomy()
        packet_by_id = {packet.packet_id: packet for packet in packets}
        snapshot_by_id = {
            snapshot.market_snapshot_id: snapshot for snapshot in snapshots
        }
        packet = packet_by_id.get(packet_id)
        if packet is None:
            raise ReviewDataError(f"unknown packet_id: {packet_id}")

        source_snapshot_id = payload.source_market_snapshot_id
        if source_snapshot_id and source_snapshot_id not in packet.market_snapshot_ids:
            raise ReviewDataError(
                "source_market_snapshot_id must be one of this packet's market_snapshot_ids"
            )
        source_snapshot = (
            snapshot_by_id.get(source_snapshot_id) if source_snapshot_id else None
        )
        if source_snapshot_id and source_snapshot is None:
            raise ReviewDataError("source_market_snapshot_id references missing snapshot")

        captured_at = datetime.now(UTC).isoformat()
        digest = stable_hash(
            {
                "packet_id": packet_id,
                "source_market_snapshot_id": source_snapshot_id,
                "captured_at_utc": captured_at,
                "market_id": payload.market_id,
                "url": payload.url,
                "title": payload.title,
                "resolution_rules": payload.resolution_rules,
            }
        )[7:23]
        snapshot_id = f"ms_{_safe_id(packet_id)}_reviewed_{digest}"
        snapshot = MarketSnapshot(
            market_snapshot_id=snapshot_id,
            market_id=payload.market_id,
            venue=payload.venue,
            url=payload.url,
            title=payload.title,
            description=payload.description.strip(),
            resolution_rules=payload.resolution_rules,
            outcomes=payload.outcomes,
            close_date=payload.close_date or None,
            status=payload.status.strip() or "open",
            captured_at_utc=captured_at,
            snapshot_kind="reviewer_edited_snapshot_v0",
            retrieval_provider="human_review",
            retrieval_id=f"review_edit:{digest}",
            retrieval_query=proposed_thesis_text(packet) or None,
            polydata_cutoff_at_utc=None,
            market_role=payload.market_role,
        )

        if snapshot.market_snapshot_id in snapshot_by_id:
            raise ReviewDataError("edited market snapshot ID collision")
        next_snapshot_ids = [
            snapshot.market_snapshot_id,
            *packet.market_snapshot_ids,
        ]
        next_snapshot_ids = _dedupe_preserving_order(next_snapshot_ids)
        edit_history = list(
            packet.source_provenance.get("reviewer_market_snapshot_edits_v0", [])
        )
        edit_history.append(
            {
                "edited_market_snapshot_id": snapshot.market_snapshot_id,
                "source_market_snapshot_id": source_snapshot_id,
                "source_market_id": (
                    source_snapshot.market_id if source_snapshot else None
                ),
                "reviewed_at_utc": captured_at,
                "retrieval_provider": snapshot.retrieval_provider,
                "snapshot_kind": snapshot.snapshot_kind,
            }
        )
        updated_packet = packet.model_copy(
            update={
                "market_snapshot_ids": next_snapshot_ids,
                "source_provenance": {
                    **packet.source_provenance,
                    "reviewer_market_snapshot_edits_v0": edit_history,
                },
            }
        )
        updated_packets = [
            updated_packet if item.packet_id == packet_id else item
            for item in packets
        ]
        updated_snapshots = [*snapshots, snapshot]
        validate_references(
            packets=updated_packets,
            snapshots=updated_snapshots,
            judgments=judgments,
            decisions=decisions,
            taxonomy=taxonomy,
        )
        write_jsonl(self.snapshots_path, updated_snapshots)
        write_jsonl(self.packets_path, updated_packets)
        return snapshot, source_snapshot_id

    def exports(self) -> dict[str, list[dict[str, Any]]]:
        packets = self.packets()
        decisions = self.decisions()
        judgments = self.judgments()
        taxonomy = self.taxonomy()
        return {
            "reference": export_reference_labels(packets, decisions, taxonomy),
            "stress": export_stress_suite(packets, decisions),
            "preferences": export_preference_pairs(packets, judgments, decisions),
            "clean_goldens": export_clean_golden_samples(
                packets, self.snapshots(), decisions, taxonomy
            ),
        }


def build_app(repository: ReviewRepository | None = None) -> FastAPI:
    repo = repository or ReviewRepository()
    app = FastAPI(title="FitCheck Review Console", version="review_dataset_v0")

    @app.get("/review", response_class=HTMLResponse)
    def review_page() -> str:
        return STATIC_HTML.read_text(encoding="utf-8")

    @app.get("/review/packets")
    def list_packets(
        include_reviewed: bool = False,
        include_incomplete: bool = False,
        include_duplicates: bool = False,
    ):
        return {
            "packets": repo.list_packets(
                include_reviewed=include_reviewed,
                include_incomplete=include_incomplete,
                include_duplicates=include_duplicates,
            )
        }

    @app.get("/review/corrections")
    def list_corrections():
        return {"packets": repo.list_corrections()}

    @app.get("/review/packets/{packet_id}")
    def packet_detail(packet_id: str):
        try:
            return repo.packet_detail(packet_id)
        except ReviewDataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/review/packets/{packet_id}/market-snapshots")
    def save_market_snapshot(packet_id: str, payload: MarketSnapshotEditInput):
        before_decisions = file_hash(repo.decisions_path)
        before_judgments = file_hash(repo.judgments_path)
        try:
            snapshot, source_snapshot_id = repo.save_market_snapshot_edit(
                packet_id, payload
            )
        except ReviewDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if before_decisions != file_hash(repo.decisions_path):
            raise HTTPException(status_code=500, detail="decision file mutated")
        if before_judgments != file_hash(repo.judgments_path):
            raise HTTPException(status_code=500, detail="candidate judgment file mutated")
        return {
            "packet_id": packet_id,
            "replaces_snapshot_id": source_snapshot_id,
            "market_snapshot": snapshot.model_dump(mode="json"),
        }

    @app.post("/review/decisions")
    def submit_decision(payload: ReviewDecisionInput):
        before = file_hash(repo.packets_path)
        try:
            decision = repo.append_decision(payload)
        except ReviewDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        after = file_hash(repo.packets_path)
        if before != after:
            raise HTTPException(status_code=500, detail="packet file mutated")
        return {"decision": decision.model_dump(mode="json")}

    @app.put("/review/decisions/{review_decision_id}")
    def update_decision(review_decision_id: str, payload: ReviewDecisionInput):
        before_packets = file_hash(repo.packets_path)
        before_snapshots = file_hash(repo.snapshots_path)
        before_judgments = file_hash(repo.judgments_path)
        before_count = len(load_decisions(repo.decisions_path, repo.taxonomy()))
        try:
            decision = repo.replace_decision(review_decision_id, payload)
        except ReviewDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        after_count = len(load_decisions(repo.decisions_path, repo.taxonomy()))
        if before_count != after_count:
            raise HTTPException(status_code=500, detail="decision count changed")
        if before_packets != file_hash(repo.packets_path):
            raise HTTPException(status_code=500, detail="packet file mutated")
        if before_snapshots != file_hash(repo.snapshots_path):
            raise HTTPException(status_code=500, detail="snapshot file mutated")
        if before_judgments != file_hash(repo.judgments_path):
            raise HTTPException(status_code=500, detail="candidate judgment file mutated")
        return {
            "decision": decision.model_dump(mode="json"),
            "updated_existing": True,
        }

    @app.get("/review/exports/reference")
    def reference_export():
        return {"rows": repo.exports()["reference"]}

    @app.get("/review/exports/stress")
    def stress_export():
        return {"rows": repo.exports()["stress"]}

    @app.get("/review/exports/preferences")
    def preferences_export():
        return {"rows": repo.exports()["preferences"]}

    @app.get("/review/exports/clean-goldens")
    def clean_goldens_export():
        rows = repo.exports()["clean_goldens"]
        return {
            "rows": rows,
            "market_snapshots": export_clean_golden_market_snapshots(rows),
        }

    @app.get("/review/health")
    def health():
        try:
            repo.validate()
        except (ReviewDataError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    return app


app = build_app()


def _duplicate_payload_index(
    packets: list[ReviewPacket],
    snapshot_by_id: dict[str, MarketSnapshot],
) -> dict[str, dict[str, Any]]:
    by_fingerprint: dict[str, list[str]] = {}
    fingerprints = {
        packet.packet_id: _review_payload_fingerprint(packet, snapshot_by_id)
        for packet in packets
    }
    for packet_id, fingerprint in fingerprints.items():
        by_fingerprint.setdefault(fingerprint, []).append(packet_id)
    return {
        packet_id: {
            "fingerprint": fingerprint,
            "packet_ids": sorted(by_fingerprint[fingerprint]),
        }
        for packet_id, fingerprint in fingerprints.items()
    }


def _review_payload_fingerprint(
    packet: ReviewPacket,
    snapshot_by_id: dict[str, MarketSnapshot],
) -> str:
    market_ids = [
        snapshot_by_id[snapshot_id].market_id
        for snapshot_id in packet.market_snapshot_ids
        if snapshot_id in snapshot_by_id
    ]
    return json.dumps(
        {
            "proposed_thesis": " ".join(proposed_thesis_text(packet).lower().split()),
            "market_ids": sorted(market_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _snapshots_for_review_display(
    packet: ReviewPacket,
    snapshot_by_id: dict[str, MarketSnapshot],
) -> list[MarketSnapshot]:
    snapshots = [
        snapshot_by_id[snapshot_id]
        for snapshot_id in packet.market_snapshot_ids
        if snapshot_id in snapshot_by_id
    ]
    best_market_id = packet.reference_candidate.get("best_market_id")
    original_index = {
        snapshot.market_snapshot_id: index for index, snapshot in enumerate(snapshots)
    }
    return sorted(
        snapshots,
        key=lambda snapshot: (
            _snapshot_review_priority(snapshot, best_market_id),
            original_index[snapshot.market_snapshot_id],
        ),
    )


def _snapshot_review_priority(
    snapshot: MarketSnapshot,
    best_market_id: str | None,
) -> int:
    if snapshot.snapshot_kind == "reviewer_edited_snapshot_v0":
        return 0
    if best_market_id and snapshot.market_id == best_market_id:
        return 1
    if snapshot.retrieval_provider != "polydata":
        return 2
    if not snapshot.snapshot_kind.startswith("polydata_"):
        return 2
    return 3


def _review_focus(
    packet: ReviewPacket,
    *,
    market_snapshots: list[MarketSnapshot],
    duplicate_packet_ids: list[str],
    thesis_peer_packet_ids: list[str],
) -> dict[str, Any]:
    reference = packet.reference_candidate
    best_market_id = reference.get("best_market_id")
    comparison = next(
        (
            snapshot
            for snapshot in market_snapshots
            if best_market_id and snapshot.market_id == best_market_id
        ),
        market_snapshots[0] if market_snapshots else None,
    )
    return {
        "source_case_id": packet.source_case_id,
        "source_scope": packet.source_scope,
        "source_dataset": packet.source_dataset,
        "current_source_fit_class": reference.get("fit_class"),
        "best_market_id": best_market_id,
        "comparison_market_snapshot_id": (
            comparison.market_snapshot_id if comparison else None
        ),
        "comparison_market_id": comparison.market_id if comparison else None,
        "comparison_market_title": comparison.title if comparison else None,
        "comparison_market_role": comparison.market_role if comparison else None,
        "source_notes": _reference_notes(reference),
        "duplicate_packet_ids": duplicate_packet_ids,
        "duplicate_count": len(duplicate_packet_ids),
        "same_thesis_packet_ids": thesis_peer_packet_ids,
        "same_thesis_count": len(thesis_peer_packet_ids),
    }


def _thesis_peer_packet_ids(
    packet: ReviewPacket,
    packets: list[ReviewPacket],
) -> list[str]:
    thesis = _normalized_thesis_for_group(packet)
    if not thesis:
        return []
    return sorted(
        candidate.packet_id
        for candidate in packets
        if _normalized_thesis_for_group(candidate) == thesis
    )


def _normalized_thesis_for_group(packet: ReviewPacket) -> str:
    return " ".join(proposed_thesis_text(packet).lower().split())


def _reference_notes(reference: dict[str, Any]) -> str | None:
    for key in (
        "notes",
        "expected_behavior",
        "minimum_expected_behavior",
        "truth_scope_from_source",
    ):
        value = reference.get(key)
        if value:
            return str(value)
    failure_modes = reference.get("failure_modes")
    if failure_modes:
        return f"Failure modes: {', '.join(map(str, failure_modes))}"
    return None


def _decision_quality_flags(decision: ReviewDecision) -> list[str]:
    flags: list[str] = []
    if (
        decision.recheck_mode == MANUAL_CORRECTION_RECHECK_MODE
        and decision.recheck_status != "cleared"
    ):
        flags.append("manual_correction_requested")
    if decision.fit_class in MARKET_REQUIRED_FIT_CLASSES and (
        decision.market_snapshot_selection != "selected"
        or not decision.selected_market_snapshot_id
    ):
        flags.append("market_fit_without_selected_snapshot")
    if decision.fit_class == "no_clean_expression" and (
        decision.market_snapshot_selection != "none"
        or decision.selected_market_snapshot_id
    ):
        flags.append("no_clean_expression_with_selected_snapshot")
    if decision.preferred_judgment_id and decision.preference_strength not in {
        "strong",
        "weak",
    }:
        flags.append("preferred_judgment_without_strength")
    if (
        not decision.preferred_judgment_id
        and decision.preference_strength in {"strong", "weak"}
    ):
        flags.append("preference_strength_without_judgment")
    return flags


def _decision_quality_severity(flags: list[str]) -> int:
    weights = {
        "manual_correction_requested": 80,
        "market_fit_without_selected_snapshot": 100,
        "no_clean_expression_with_selected_snapshot": 100,
        "preferred_judgment_without_strength": 60,
        "preference_strength_without_judgment": 60,
    }
    return max(weights.get(flag, 0) for flag in flags)


def _collapse_duplicate_payloads(
    *,
    queue: list[ReviewPacket],
    all_packets: list[ReviewPacket],
    latest: dict[str, ReviewDecision],
    duplicate_info: dict[str, dict[str, Any]],
) -> list[ReviewPacket]:
    reviewed_fingerprints = {
        duplicate_info[packet_id]["fingerprint"]
        for packet_id in latest
        if packet_id in duplicate_info
    }
    first_by_fingerprint = {
        packet.packet_id: min(duplicate_info[packet.packet_id]["packet_ids"])
        for packet in all_packets
    }
    kept: list[ReviewPacket] = []
    seen: set[str] = set()
    for packet in queue:
        fingerprint = duplicate_info[packet.packet_id]["fingerprint"]
        if fingerprint in reviewed_fingerprints:
            continue
        if first_by_fingerprint[packet.packet_id] != packet.packet_id:
            continue
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        kept.append(packet)
    return kept


def _has_rejected_parent(
    packet: ReviewPacket,
    rejected_packets: set[str],
) -> bool:
    parent_id = packet.source_provenance.get("live_snapshot_fill_parent_packet_id")
    if not parent_id:
        return False
    return str(parent_id) in rejected_packets


def _rejected_payload_fingerprints(
    rejected_packets: set[str],
    duplicate_info: dict[str, dict[str, Any]],
) -> set[str]:
    return {
        duplicate_info[packet_id]["fingerprint"]
        for packet_id in rejected_packets
        if packet_id in duplicate_info
    }


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return safe[:96] or "id"


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
