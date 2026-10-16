"""Bounded, source-only admission for the production MCP pilot."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from el.domain.tables import ApiClient, Job
from el.mcp.auth import Principal
from el.mcp.contracts import PermissionDenied

PILOT_TIER = "source-pilot-v1"
PILOT_TOOLS = frozenset({
    "v3_submit_source_interpretation",
    "v3_process_source_interpretation",
    "v3_get_source_interpretation_job",
    "v3_get_source_interpretation_job_by_idempotency",
})


@dataclass(frozen=True)
class SourcePilotPolicy:
    actor_id: uuid.UUID
    max_jobs: int
    expires_at: datetime

    @classmethod
    def from_env(cls) -> "SourcePilotPolicy":
        try:
            actor = uuid.UUID(os.environ["FITCHECK_SOURCE_PILOT_ACTOR_ID"])
            raw_max = os.environ["FITCHECK_SOURCE_PILOT_MAX_JOBS"]
            if not raw_max.isdecimal() or int(raw_max) < 1:
                raise ValueError("max jobs must be a positive integer")
            maximum = int(raw_max)
            raw_expiry = os.environ["FITCHECK_SOURCE_PILOT_EXPIRES_AT"]
            expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.utcoffset() != timezone.utc.utcoffset(None):
                raise ValueError("expiry must be UTC")
        except (KeyError, ValueError) as exc:
            raise ValueError("source pilot actor, positive max jobs, and UTC expiry are required") from exc
        return cls(actor_id=actor, max_jobs=maximum, expires_at=expiry)

    def authorize_tool(self, principal: Principal, name: str) -> Principal:
        is_pilot = (
            principal.actor_id == str(self.actor_id)
            and principal.rate_limit_tier == PILOT_TIER
        )
        if is_pilot and name in PILOT_TOOLS:
            return principal
        if (
            principal.actor_id != str(self.actor_id)
            and principal.rate_limit_tier != PILOT_TIER
            and not name.startswith("v3_")
        ):
            return principal
        raise PermissionDenied("tool unavailable for this API client")

    def lock_client(
        self, session: Session, *, actor_id: str, client_type: str,
        user_id: uuid.UUID,
    ) -> ApiClient:
        if actor_id != str(self.actor_id):
            raise PermissionDenied("source pilot client is not authorized")
        client = session.scalar(
            select(ApiClient).where(ApiClient.id == self.actor_id).with_for_update()
        )
        if (
            client is None or client.revoked_at is not None
            or client.client_type != client_type
            or client.user_id != user_id
            or client.rate_limit_tier != PILOT_TIER
        ):
            raise PermissionDenied("source pilot client is not authorized")
        return client

    def is_current(self, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc) < self.expires_at

    def allow_effect(self, session: Session, job: Job, now: datetime) -> bool:
        if not self.is_current(now) or job.owner_actor_id != str(self.actor_id):
            return False
        client = session.scalar(
            select(ApiClient).where(ApiClient.id == self.actor_id).with_for_update()
        )
        return bool(
            client is not None
            and client.revoked_at is None
            and client.rate_limit_tier == PILOT_TIER
            and client.client_type == job.owner_client_type
            and client.user_id == job.owner_user_id
        )
