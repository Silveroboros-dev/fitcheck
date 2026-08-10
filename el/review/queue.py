"""Loop 4 — minimal review queue over review_candidates (Step 8).

Human-gated triage of the review_candidates that the MCP corrections + eval
failures produce: list the pending queue, see counts, record an accept/reject
decision.

Boundary (deliberate): ACCEPTED means "ready for promotion review" — it does
NOT promote. Turning an accepted correction into a governed change stays the
separate, verifier-gated ``el.review.promotion`` path (GO + artifact). This
module NEVER calls into promotion; there is no auto-promote.

Schema option (a): no migration. Metrics slice by the FIRST-CLASS columns only
(status, source, object_type). ``failure_family`` / ``actor_id`` / ``client_type``
live inside the ``reviewer_notes`` JSON blob and are NOT treated as authoritative
here — parsing them for analytics would pretend a structure the schema does not
guarantee. A first-class column for those is a later migration (out of scope).
"""

import argparse
import datetime
import json
import sys
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.db import make_session_factory
from el.domain.enums import ReviewStatus
from el.domain.tables import ReviewCandidate

_DECISIONS = {"accept": ReviewStatus.ACCEPTED, "reject": ReviewStatus.REJECTED}


class QueueError(Exception):
    """Unknown candidate, invalid decision, or a conflicting second decision."""


@dataclass(frozen=True)
class QueueItem:
    id: str
    object_type: str
    object_id: str
    source: str
    status: str
    reviewer_notes: str | None
    created_at: str | None
    decided_at: str | None


def _item(c: ReviewCandidate) -> QueueItem:
    return QueueItem(
        id=str(c.id),
        object_type=c.object_type,
        object_id=str(c.object_id),
        source=c.source,
        status=c.status,
        reviewer_notes=c.reviewer_notes,
        created_at=c.created_at.isoformat() if c.created_at else None,
        decided_at=c.decided_at.isoformat() if c.decided_at else None,
    )


def list_pending(
    session_factory: sessionmaker[Session],
    *,
    source: str | None = None,
    object_type: str | None = None,
    limit: int | None = None,
) -> list[QueueItem]:
    """Pending candidates, oldest first, optionally filtered by source /
    object_type (first-class columns only)."""
    with session_factory() as s:
        q = select(ReviewCandidate).where(
            ReviewCandidate.status == ReviewStatus.PENDING.value
        )
        if source is not None:
            q = q.where(ReviewCandidate.source == source)
        if object_type is not None:
            q = q.where(ReviewCandidate.object_type == object_type)
        q = q.order_by(ReviewCandidate.created_at.asc())
        if limit is not None:
            q = q.limit(limit)
        return [_item(c) for c in s.scalars(q)]


def decide(
    session_factory: sessionmaker[Session],
    candidate_id: uuid.UUID | str,
    decision: str,
    reviewer_notes: str | None = None,
) -> QueueItem:
    """Record a human review decision. ``decision`` in {'accept', 'reject'}.

    Valid transition is pending -> accepted | rejected. Repeating the SAME
    decision is idempotent (no change; the original decided_at is preserved). A
    CONFLICTING second decision on an already-decided candidate raises
    QueueError. ACCEPTED = ready for promotion review; this never promotes.
    """
    if decision not in _DECISIONS:
        raise QueueError(f"decision must be 'accept' or 'reject', got {decision!r}")
    target = _DECISIONS[decision]
    cid = uuid.UUID(str(candidate_id))
    with session_factory() as s:
        c = s.get(ReviewCandidate, cid)
        if c is None:
            raise QueueError(f"review_candidate {cid} not found")
        if c.status == ReviewStatus.PENDING.value:
            c.status = target.value
            c.decided_at = datetime.datetime.now(datetime.timezone.utc)
            if reviewer_notes:
                sep = "\n" if c.reviewer_notes else ""
                c.reviewer_notes = f"{c.reviewer_notes or ''}{sep}[review:{decision}] {reviewer_notes}"
            s.commit()
            return _item(c)
        if c.status == target.value:
            # Idempotent: the same decision again is a no-op (decided_at kept).
            return _item(c)
        # Already decided the OTHER way — a conflicting decision is refused.
        raise QueueError(
            f"review_candidate {cid} is already {c.status}; cannot {decision} it"
        )


@dataclass(frozen=True)
class QueueMetrics:
    total: int
    by_status: dict[str, int]
    by_source: dict[str, int]
    by_object_type: dict[str, int]
    by_status_source: dict[str, dict[str, int]]


def metrics(session_factory: sessionmaker[Session]) -> QueueMetrics:
    """Queue counts over the first-class columns only (no reviewer_notes
    parsing)."""
    with session_factory() as s:

        def grouped(col) -> dict[str, int]:
            return {
                k: n for k, n in s.execute(select(col, func.count()).group_by(col)).all()
            }

        by_status = grouped(ReviewCandidate.status)
        by_source = grouped(ReviewCandidate.source)
        by_object_type = grouped(ReviewCandidate.object_type)
        by_status_source: dict[str, dict[str, int]] = {}
        for st, src, n in s.execute(
            select(
                ReviewCandidate.status, ReviewCandidate.source, func.count()
            ).group_by(ReviewCandidate.status, ReviewCandidate.source)
        ).all():
            by_status_source.setdefault(st, {})[src] = n
        return QueueMetrics(
            total=sum(by_status.values()),
            by_status=by_status,
            by_source=by_source,
            by_object_type=by_object_type,
            by_status_source=by_status_source,
        )


# --- CLI: python -m el.review.queue {list|metrics|decide} ------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m el.review.queue")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list pending review candidates")
    pl.add_argument("--source")
    pl.add_argument("--object-type", dest="object_type")
    pl.add_argument("--limit", type=int)

    sub.add_parser("metrics", help="queue counts by status/source/object_type")

    pd = sub.add_parser("decide", help="record an accept/reject decision")
    pd.add_argument("id")
    g = pd.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true")
    g.add_argument("--reject", action="store_true")
    pd.add_argument("--notes")
    return p


def main(argv: list[str] | None = None, session_factory: sessionmaker[Session] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    sf = session_factory or make_session_factory()
    try:
        if args.cmd == "list":
            items = list_pending(
                sf, source=args.source, object_type=args.object_type, limit=args.limit
            )
            print(json.dumps([asdict(i) for i in items], indent=2))
            print(f"# {len(items)} pending", file=sys.stderr)
        elif args.cmd == "metrics":
            print(json.dumps(asdict(metrics(sf)), indent=2))
        elif args.cmd == "decide":
            decision = "accept" if args.accept else "reject"
            item = decide(sf, args.id, decision, reviewer_notes=args.notes)
            print(json.dumps(asdict(item), indent=2))
    except QueueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
