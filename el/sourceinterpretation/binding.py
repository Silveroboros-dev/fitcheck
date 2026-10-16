"""Shared fail-closed validation for persisted source-job lineage."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from el.domain.tables import Job, SourceInterpretation, SourceInterpretationRequest


SOURCE_INTERPRETATION_JOB_TYPE = "source_interpretation_v1"


class DurableSourceBindingError(RuntimeError):
    """Persisted async source evidence no longer matches its durable job."""


def require_durable_source_binding(
    session: Session,
    interpretation: SourceInterpretation,
    *,
    owner_client_type: str,
    owner_actor_id: str,
    owner_user_id: uuid.UUID,
    agent_client_id: str,
) -> SourceInterpretationRequest | None:
    """Validate optional async request/job lineage and return its request.

    Direct legacy interpretations intentionally have neither reference.  Once
    either async reference exists, the complete owner, request, successful-job,
    payload and result binding is required.
    """

    request_id = interpretation.source_interpretation_request_id
    job_id = interpretation.job_id
    if (
        interpretation.client_type != owner_client_type
        or interpretation.agent_client_id != agent_client_id
    ):
        raise DurableSourceBindingError("source owner binding is invalid")
    if request_id is None and job_id is None:
        return None
    if request_id is None or job_id is None:
        raise DurableSourceBindingError("async source binding is partial")

    request = session.get(SourceInterpretationRequest, request_id)
    job = session.get(Job, job_id)
    if (
        request is None
        or job is None
        or request.owner_client_type != owner_client_type
        or request.owner_actor_id != owner_actor_id
        or request.agent_client_id != agent_client_id
        or request.input_digest != interpretation.input_digest
        or request.input_text != interpretation.input_text
        or request.source_url != interpretation.source_url
        or job.job_type != SOURCE_INTERPRETATION_JOB_TYPE
        or job.owner_client_type != owner_client_type
        or job.owner_actor_id != owner_actor_id
        or job.owner_user_id != owner_user_id
        or job.status != "succeeded"
        or not isinstance(job.payload, dict)
        or job.payload.get("source_interpretation_request_id") != str(request.id)
        or job.payload.get("input_digest") != interpretation.input_digest
        or not isinstance(job.result, dict)
        or job.result.get("source_interpretation_id") != str(interpretation.id)
        or job.result.get("source_interpretation_request_id") != str(request.id)
        or job.result.get("input_digest") != interpretation.input_digest
    ):
        raise DurableSourceBindingError("durable source binding is invalid")
    return request
