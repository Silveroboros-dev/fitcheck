"""Deterministic overlapping saved-revisit deliveries, with no provider calls."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from el.domain.tables import (
    Base, MonitoringObservation, PersonalThesis, RevisitCapture, RevisitPacket, RevisitRequest,
)
from el.product.api import ProductApi
from el.product.app import build_app
from el.product.wiring import build_services, local_actor
from el.revisit.service import RevisitConflict, RevisitNotFound, RevisitService

CLAIM = "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by the end of 2026."


@pytest.fixture()
def revisit_sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'revisit-race.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _activated(sessions):
    services = build_services(mode="fixture", session_factory=sessions)
    actor = local_actor(sessions)
    api = ProductApi(services, actor)
    client = TestClient(build_app(api))
    proposal = client.post("/api/v3/normalization-attempts", json={"input_text": CLAIM}).json()
    accepted = client.post(
        f"/api/v3/normalization-attempts/{proposal['normalization_attempt_id']}/accept",
        json={"expected_input_digest": proposal["input_digest"]},
    )
    assert accepted.status_code == 200, accepted.text
    pool = client.post(f"/api/v3/theses/{accepted.json()['thesis_analysis_id']}/market-pool")
    assert pool.status_code == 200, pool.text
    data = pool.json()
    choice = client.post(
        f"/api/v3/market-pools/{data['market_display_set_id']}/choice",
        json={"selection_kind": "market", "market_assessment_id": data["candidate_markets"][0]["market_assessment_id"]},
    )
    assert choice.status_code == 200, choice.text
    activation = client.post(f"/api/v3/market-choices/{choice.json()['market_choice_id']}/revisit-activation")
    assert activation.status_code == 200, activation.text
    return api._revisit, actor, activation.json()


def _capture(service, activation, label):
    baseline = activation["baseline"]
    return service.register_capture(
        uuid.UUID(activation["binding"]["revision_id"]),
        source_kind="synthetic_fixture", source_ref=f"fixture:idempotency:{label}",
        observed_at=datetime.fromisoformat(baseline["snapshot_as_of"]) + timedelta(days=1),
        market_id=baseline["market_id"],
        evidence={"market_id": baseline["market_id"], "title": baseline["title"],
                  "rules": baseline["rules"] + f"\nSynthetic later clause: {label}.",
                  "deadline": None, "lifecycle": None, "condition_id": None},
    )


def _overlap(sessions, actor, revision_id, *, boundary, winner_input, loser_input, after_winner=None):
    """Pause the losing delivery after a real empty SELECT, then commit its peer.

    Separate sessions/connections execute production service code. No database
    error or result is mocked: the losing INSERT reaches the real UNIQUE key.
    """
    lookup_done = threading.Event()
    winner_done = threading.Event()
    role = threading.local()

    class PausedSession(Session):
        def _pause_for_winner(self):
            role.pause = False
            lookup_done.set()
            assert winner_done.wait(timeout=10), "winning delivery did not finish"

        def scalar(self, statement, *args, **kwargs):
            result = super().scalar(statement, *args, **kwargs)
            entities = getattr(statement, "column_descriptions", ())
            if (getattr(role, "pause", False) and result is None and entities
                and boundary is not MonitoringObservation
                and entities[0].get("entity") is boundary):
                self._pause_for_winner()
            return result

        def flush(self, *args, **kwargs):
            # The service checks equivalent comparisons after the direct
            # observation lookup. Wait until every lookup chose an INSERT, so
            # this boundary reaches the observation UNIQUE key itself.
            if (getattr(role, "pause", False) and boundary is MonitoringObservation
                and any(isinstance(row, MonitoringObservation) for row in self.new)):
                self._pause_for_winner()
            return super().flush(*args, **kwargs)

    racing = sessionmaker(bind=sessions.kw["bind"], class_=PausedSession, expire_on_commit=False)
    service = RevisitService(
        racing, user_id=actor.user_id, actor_id=actor.actor_id,
        agent_client_id=actor.agent_client_id, mode="fixture",
    )

    def losing_delivery():
        role.pause = True
        return service.check(revision_id, idempotency_key="overlap", **loser_input)

    with ThreadPoolExecutor(max_workers=1) as pool:
        losing = pool.submit(losing_delivery)
        try:
            assert lookup_done.wait(timeout=10), "losing delivery did not reach the empty lookup"
            winner = service.check(revision_id, idempotency_key="overlap", **winner_input)
            if after_winner is not None:
                after_winner()
        finally:
            winner_done.set()
        return winner, losing.result(timeout=10)


def _count(sessions, table, revision_id):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(table).where(table.revision_id == revision_id))


def assert_overlapping_check(sessions, boundary_name):
    """Shared SQLite/PostgreSQL regression against each existing insert boundary."""
    service, actor, activation = _activated(sessions)
    revision_id = uuid.UUID(activation["binding"]["revision_id"])
    if boundary_name == "capture":
        boundary = RevisitCapture
        pinned = {"fixture_scenario": "rules_added"}
        expected_requests = 1
    else:
        capture_id = _capture(service, activation, "same-input")
        pinned = {"capture_id": capture_id}
        boundary = MonitoringObservation if boundary_name == "observation" else RevisitRequest
        expected_requests = 1
        if boundary_name == "request":
            service.check(revision_id, idempotency_key="seed", **pinned)
            expected_requests = 2
    winner, loser = _overlap(
        sessions, actor, revision_id, boundary=boundary,
        winner_input=pinned, loser_input=pinned,
    )
    assert loser == winner
    assert loser["binding"] == activation["binding"]
    assert _count(sessions, RevisitCapture, revision_id) == 1
    assert _count(sessions, MonitoringObservation, revision_id) == 1
    assert _count(sessions, RevisitPacket, revision_id) == 1
    assert _count(sessions, RevisitRequest, revision_id) == expected_requests


def assert_overlapping_conflicting_input(sessions):
    service, actor, activation = _activated(sessions)
    revision_id = uuid.UUID(activation["binding"]["revision_id"])
    first = _capture(service, activation, "first")
    second = _capture(service, activation, "different")
    for index, capture_id in enumerate((first, second)):
        service.check(revision_id, idempotency_key=f"seed-{index}", capture_id=capture_id)
    with pytest.raises(RevisitConflict, match="different pinned input"):
        _overlap(
            sessions, actor, revision_id, boundary=RevisitRequest,
            winner_input={"capture_id": first}, loser_input={"capture_id": second},
        )
    assert _count(sessions, MonitoringObservation, revision_id) == 2
    assert _count(sessions, RevisitPacket, revision_id) == 2
    assert _count(sessions, RevisitRequest, revision_id) == 3
    with sessions() as session:
        row = session.scalar(select(RevisitRequest).where(
            RevisitRequest.revision_id == revision_id,
            RevisitRequest.idempotency_key == "overlap",
        ))
        assert row.capture_id == first


@pytest.mark.parametrize("boundary_name", ["request", "observation", "capture"])
def test_overlapping_check_returns_one_bound_result(revisit_sessions, boundary_name):
    assert_overlapping_check(revisit_sessions, boundary_name)


def test_overlapping_key_reuse_with_different_input_is_conflict(revisit_sessions):
    assert_overlapping_conflicting_input(revisit_sessions)


def test_collision_retry_rechecks_active_revision(revisit_sessions):
    service, actor, activation = _activated(revisit_sessions)
    revision_id = uuid.UUID(activation["binding"]["revision_id"])
    capture_id = _capture(service, activation, "active-binding")
    service.check(revision_id, idempotency_key="seed", capture_id=capture_id)

    def deactivate():
        with revisit_sessions() as session:
            thesis = session.get(PersonalThesis, uuid.UUID(activation["binding"]["personal_thesis_id"]))
            thesis.active_revision_id = None
            session.commit()

    with pytest.raises(RevisitConflict, match="no longer active"):
        _overlap(
            revisit_sessions, actor, revision_id, boundary=RevisitRequest,
            winner_input={"capture_id": capture_id}, loser_input={"capture_id": capture_id},
            after_winner=deactivate,
        )
    assert _count(revisit_sessions, RevisitRequest, revision_id) == 2


@pytest.mark.parametrize("foreign_binding", ["user", "actor", "revision"])
def test_existing_request_does_not_disclose_foreign_binding(revisit_sessions, foreign_binding):
    service, actor, activation = _activated(revisit_sessions)
    revision_id = uuid.UUID(activation["binding"]["revision_id"])
    capture_id = _capture(service, activation, "ownership")
    service.check(revision_id, idempotency_key="saved", capture_id=capture_id)
    foreign = RevisitService(
        revisit_sessions,
        user_id=uuid.uuid4() if foreign_binding == "user" else actor.user_id,
        actor_id="foreign" if foreign_binding == "actor" else actor.actor_id,
        agent_client_id=actor.agent_client_id, mode="fixture",
    )
    with pytest.raises(RevisitNotFound):
        foreign.check(
            uuid.uuid4() if foreign_binding == "revision" else revision_id,
            idempotency_key="saved", capture_id=capture_id,
        )
    assert _count(revisit_sessions, RevisitRequest, revision_id) == 1


def test_unrelated_integrity_error_is_not_retried_and_rolls_back(revisit_sessions):
    service, actor, activation = _activated(revisit_sessions)
    revision_id = uuid.UUID(activation["binding"]["revision_id"])
    capture_id = _capture(service, activation, "unrelated-error")
    attempts = []

    class InvalidSession(Session):
        def __init__(self, *args, **kwargs):
            attempts.append(None)
            super().__init__(*args, **kwargs)

    @event.listens_for(InvalidSession, "before_flush")
    def invalidate_request(session, _context, _instances):
        for row in session.new:
            if isinstance(row, RevisitRequest):
                row.request_digest = None

    invalid = RevisitService(
        sessionmaker(bind=revisit_sessions.kw["bind"], class_=InvalidSession, expire_on_commit=False),
        user_id=actor.user_id, actor_id=actor.actor_id,
        agent_client_id=actor.agent_client_id, mode="fixture",
    )
    with pytest.raises(IntegrityError) as failure:
        invalid.check(revision_id, idempotency_key="invalid", capture_id=capture_id)
    assert failure.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_NOTNULL"
    assert len(attempts) == 1
    assert _count(revisit_sessions, MonitoringObservation, revision_id) == 0
    assert _count(revisit_sessions, RevisitPacket, revision_id) == 0
    assert _count(revisit_sessions, RevisitRequest, revision_id) == 0


def test_collision_retry_is_bounded(revisit_sessions, monkeypatch):
    import sqlite3

    service, _, _ = _activated(revisit_sessions)
    attempts = []
    original = sqlite3.IntegrityError(
        "UNIQUE constraint failed: revisit_requests.user_id, "
        "revisit_requests.revision_id, revisit_requests.idempotency_key"
    )
    original.sqlite_errorname = "SQLITE_CONSTRAINT_UNIQUE"
    failure = IntegrityError("INSERT INTO revisit_requests", {}, original)

    def always_collides(*args, **kwargs):
        attempts.append(None)
        raise failure

    monkeypatch.setattr(service, "_check_once", always_collides)
    with pytest.raises(IntegrityError) as caught:
        service.check(uuid.uuid4(), idempotency_key="bounded", capture_id=uuid.uuid4())
    assert caught.value is failure
    assert len(attempts) == 4


def test_named_postgres_error_requires_unique_violation(revisit_sessions, monkeypatch):
    from types import SimpleNamespace

    service, _, _ = _activated(revisit_sessions)
    attempts = []

    class NonUniqueDatabaseError(Exception):
        sqlstate = "23514"
        diag = SimpleNamespace(constraint_name="uq_revisit_request_key")

    failure = IntegrityError("INSERT INTO revisit_requests", {}, NonUniqueDatabaseError())

    def fails(*args, **kwargs):
        attempts.append(None)
        raise failure

    monkeypatch.setattr(service, "_check_once", fails)
    with pytest.raises(IntegrityError) as caught:
        service.check(uuid.uuid4(), idempotency_key="not-unique", capture_id=uuid.uuid4())
    assert caught.value is failure
    assert len(attempts) == 1
