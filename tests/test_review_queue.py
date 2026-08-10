"""Step 8 — minimal review queue (el.review.queue)."""

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from el.domain.tables import Base, ReviewCandidate
from el.review.queue import QueueError, decide, list_pending, main, metrics


def _sessions():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(sf, *, object_type="fit_card", source="agent_correction") -> uuid.UUID:
    with sf() as s:
        c = ReviewCandidate(
            object_type=object_type,
            object_id=uuid.uuid4(),
            source=source,
            status="pending",
        )
        s.add(c)
        s.commit()
        return c.id


def _status(sf, cid):
    with sf() as s:
        return s.get(ReviewCandidate, cid).status


def _decided_at(sf, cid):
    with sf() as s:
        return s.get(ReviewCandidate, cid).decided_at


def test_pending_candidate_appears_in_list():
    sf = _sessions()
    cid = _seed(sf)
    items = list_pending(sf)
    assert [i.id for i in items] == [str(cid)]
    assert items[0].status == "pending" and items[0].decided_at is None


def test_accept_sets_status_and_decided_at():
    sf = _sessions()
    cid = _seed(sf)
    out = decide(sf, cid, "accept", reviewer_notes="looks real")
    assert out.status == "accepted"
    assert out.decided_at is not None
    assert _status(sf, cid) == "accepted"
    # accepted candidate drops out of the pending queue
    assert list_pending(sf) == []


def test_reject_sets_status_and_decided_at():
    sf = _sessions()
    cid = _seed(sf)
    out = decide(sf, cid, "reject", reviewer_notes="noise")
    assert out.status == "rejected"
    assert out.decided_at is not None
    assert _status(sf, cid) == "rejected"


def test_same_decision_is_idempotent():
    sf = _sessions()
    cid = _seed(sf)
    decide(sf, cid, "accept")
    before = _decided_at(sf, cid)
    again = decide(sf, cid, "accept")  # no error
    after = _decided_at(sf, cid)
    assert again.status == "accepted"
    assert after == before  # decided_at unchanged by the idempotent re-decision


def test_conflicting_decision_fails():
    sf = _sessions()
    cid = _seed(sf)
    decide(sf, cid, "accept")
    with pytest.raises(QueueError):
        decide(sf, cid, "reject")
    assert _status(sf, cid) == "accepted"  # unchanged


def test_decide_unknown_id_and_bad_decision_fail():
    sf = _sessions()
    with pytest.raises(QueueError):
        decide(sf, uuid.uuid4(), "accept")
    with pytest.raises(QueueError):
        decide(sf, _seed(sf), "maybe")


def test_metrics_update_after_decisions():
    sf = _sessions()
    a = _seed(sf, object_type="fit_card", source="agent_correction")
    _seed(sf, object_type="fit_card", source="agent_correction")
    b = _seed(sf, object_type="market_rejection", source="agent_rejection")

    m0 = metrics(sf)
    assert m0.total == 3
    assert m0.by_status == {"pending": 3}
    assert m0.by_source == {"agent_correction": 2, "agent_rejection": 1}
    assert m0.by_object_type == {"fit_card": 2, "market_rejection": 1}

    decide(sf, a, "accept")
    decide(sf, b, "reject")
    m1 = metrics(sf)
    assert m1.by_status == {"pending": 1, "accepted": 1, "rejected": 1}
    assert m1.by_status_source["accepted"] == {"agent_correction": 1}
    assert m1.by_status_source["rejected"] == {"agent_rejection": 1}


def test_cli_smoke(capsys):
    sf = _sessions()
    cid = _seed(sf)

    assert main(["list"], session_factory=sf) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == str(cid)

    assert main(["decide", str(cid), "--accept", "--notes", "ok"], session_factory=sf) == 0
    decided = json.loads(capsys.readouterr().out)
    assert decided["status"] == "accepted"

    assert main(["metrics"], session_factory=sf) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["by_status"] == {"accepted": 1}

    # conflicting decision via CLI -> non-zero exit, error on stderr
    assert main(["decide", str(cid), "--reject"], session_factory=sf) == 1
    assert "already accepted" in capsys.readouterr().err
