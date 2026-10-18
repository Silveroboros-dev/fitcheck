"""Request-bound anonymous fixture identities for the local demo only."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from el.domain.enums import ClientType
from el.domain.tables import DemoSession, User
from el.product.demo_admission import (
    DemoAdmission, DemoAdmissionDenied, MAX_ACTIVE_SESSIONS,
    MAX_LIFETIME_SESSIONS,
)
from el.product.wiring import HumanActor

COOKIE_NAME = "fitcheck_demo"
SESSION_SECONDS = 60 * 60 * 12


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _actor(user_id: uuid.UUID) -> HumanActor:
    return HumanActor(
        user_id=user_id,
        client_type=ClientType.HUMAN_UI,
        actor_id=f"user:{user_id}",
        agent_client_id=f"human_ui:{user_id}",
    )


class DemoSessions:
    def __init__(self, sessions: sessionmaker[Session], admission: DemoAdmission | None = None):
        self._sessions = sessions
        self._admission = admission

    def resolve(self, raw_cookie: str | None) -> tuple[HumanActor, str] | None:
        if not raw_cookie or len(raw_cookie) > 128:
            return None
        token_hash = sha256(raw_cookie.encode("ascii", errors="ignore")).hexdigest()
        with self._sessions() as s:
            row = s.scalar(select(DemoSession).where(DemoSession.token_hash == token_hash))
            if row is None or row.revoked_at is not None or _aware(row.expires_at) <= _now():
                return None
            return _actor(row.user_id), str(row.id)

    def create(self, replacing_cookie: str | None = None) -> tuple[HumanActor, str, str]:
        token = secrets.token_urlsafe(32)
        with self._sessions.begin() as s:
            old = None
            now = _now()
            if self._admission is not None:
                self._admission.lock(s)
                now = self._admission.require_transaction_writable(s)
                if replacing_cookie:
                    old = s.scalar(select(DemoSession).where(
                        DemoSession.token_hash == sha256(
                            replacing_cookie.encode("ascii", errors="ignore")
                        ).hexdigest()
                    ))
                    if (old is None or old.revoked_at is not None
                        or _aware(old.expires_at) <= now):
                        raise DemoAdmissionDenied("session_expired", 401)
                active = s.scalar(select(func.count()).select_from(DemoSession).where(
                    DemoSession.revoked_at.is_(None), DemoSession.expires_at > now,
                )) or 0
                lifetime = s.scalar(select(func.count()).select_from(DemoSession)) or 0
                replacing_active = bool(old is not None and old.revoked_at is None
                                        and _aware(old.expires_at) > now)
                if lifetime >= MAX_LIFETIME_SESSIONS:
                    raise DemoAdmissionDenied("session_lifetime_limit")
                if active - int(replacing_active) >= MAX_ACTIVE_SESSIONS:
                    raise DemoAdmissionDenied("session_active_limit")
            user = User(email=f"demo-{uuid.uuid4().hex}@fitcheck.local")
            s.add(user)
            s.flush()
            row = DemoSession(
                user_id=user.id,
                token_hash=sha256(token.encode("ascii")).hexdigest(),
                expires_at=now + timedelta(seconds=SESSION_SECONDS),
            )
            s.add(row)
            s.flush()
            if old is not None and old.revoked_at is None:
                old.revoked_at = now
            actor, session_ref = _actor(user.id), str(row.id)
        return actor, session_ref, token

    def revoke(self, raw_cookie: str) -> None:
        token_hash = sha256(raw_cookie.encode("ascii", errors="ignore")).hexdigest()
        with self._sessions.begin() as s:
            row = s.scalar(select(DemoSession).where(DemoSession.token_hash == token_hash))
            if row is not None and row.revoked_at is None:
                row.revoked_at = _now()
