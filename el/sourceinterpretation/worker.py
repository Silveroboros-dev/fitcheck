"""Local separate-process entrypoint for durable source interpretation jobs."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid

from el.domain.db import make_session_factory as make_core_session_factory
from el.mcp.wiring import (
    build_fixture_v3_source_worker_services,
    build_gemini_v3_source_worker_services,
)
from el.product.wiring import build_services
from el.sourceinterpretation.jobs import SourceInterpretationWorker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="claim at most one job")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="keep polling with this idle interval",
    )
    parser.add_argument("--lease-seconds", type=int, default=90)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument(
        "--surface",
        choices=("product", "mcp", "mcp-fixture"),
        default="product",
        help=(
            "product uses the local UI composition; mcp uses the production "
            "core database and Gemini-v3 pins; mcp-fixture is the offline "
            "synthetic MCP reference."
        ),
    )
    return parser


def build_worker(
    *, surface: str, worker_id: str, lease_seconds: int
) -> SourceInterpretationWorker:
    """Compose a worker whose database and pins match its submit surface."""
    if surface == "product":
        services = build_services()
        jobs = services.jobs
        source_interpretation = services.source_interpretation
        sessions = services.session_factory
    elif surface == "mcp":
        sessions = make_core_session_factory()
        jobs, source_interpretation = build_gemini_v3_source_worker_services(sessions)
    elif surface == "mcp-fixture":
        # This surface deliberately shares the MCP core resolver. Its caller
        # must migrate the disposable database before submit, worker, and poll.
        sessions = make_core_session_factory()
        jobs, source_interpretation = build_fixture_v3_source_worker_services(
            sessions
        )
    else:  # argparse constrains CLI input; keep the callable fail-closed too.
        raise ValueError(f"unknown source worker surface: {surface!r}")
    return SourceInterpretationWorker(
        jobs=jobs,
        source_interpretation=source_interpretation,
        session_factory=sessions,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.poll_seconds is not None and args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be positive")
    worker = build_worker(
        surface=args.surface,
        worker_id=(
            args.worker_id or f"source-worker:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        ),
        lease_seconds=args.lease_seconds,
    )
    while True:
        result = worker.run_once()
        print(json.dumps(result.model_dump(mode="json"), sort_keys=True), flush=True)
        if args.once or args.poll_seconds is None:
            return 0
        if not result.claimed:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
