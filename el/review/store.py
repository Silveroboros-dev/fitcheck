"""Review-candidate persistence — the DB boundary for Loop 4 intake.

The only module here that imports SQLAlchemy/tables, so candidates.py stays
trivially testable and model/DB-free. Idempotent on
(object_type, object_id, source): re-running the eval never duplicates rows.

object_id is `Uuid` in the schema; we derive a deterministic uuid5 from the
candidate FINGERPRINT (which binds the full governing context — gate / token /
alias / eval-pack / schema versions), not from the bare ref. So a candidate
from one policy generation never dedupes against another generation of the
same ref; only an identical (ref + full context) re-collection dedupes. The
human ref + signal live in reviewer_notes. Deriving object_id this way is an
explicit THIN-SLICE shortcut to avoid a migration — a first-class
(ref, fingerprint) column pair is a Loop-4 follow-up.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.enums import ReviewStatus
from el.domain.tables import ReviewCandidate
from el.review.candidates import ReviewCandidateSpec

# Fixed namespace -> object_id is a deterministic function of the fingerprint.
_REVIEW_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "review.fitcheck.epistemic-ledger")


def candidate_object_id(fingerprint: str) -> uuid.UUID:
    return uuid.uuid5(_REVIEW_NS, fingerprint)


def persist_candidates(
    session_factory: sessionmaker[Session], specs: list[ReviewCandidateSpec]
) -> list[uuid.UUID]:
    """Upsert specs as PENDING review_candidates; returns ids of rows newly
    written (idempotent — existing (type, id, source) rows are skipped)."""
    written: list[uuid.UUID] = []
    with session_factory() as session:
        for spec in specs:
            object_id = candidate_object_id(spec.fingerprint)
            existing = session.execute(
                select(ReviewCandidate.id).where(
                    ReviewCandidate.object_type == spec.object_type,
                    ReviewCandidate.object_id == object_id,
                    ReviewCandidate.source == spec.source,
                )
            ).first()
            if existing is not None:
                continue
            row = ReviewCandidate(
                object_type=spec.object_type,
                object_id=object_id,
                source=spec.source,
                status=ReviewStatus.PENDING.value,
                reviewer_notes=spec.signal_detail,
            )
            session.add(row)
            session.flush()
            written.append(row.id)
        session.commit()
    return written
