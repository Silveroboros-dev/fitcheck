"""One-command, fixture-only local demo web and source-job worker."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path

from el.product.wiring import make_session_factory


REQUIRED_RESOURCES = (
    ("el.product", "static/product_console.html"),
    ("el.product", "static/brand/fitcheck-logo-on-dark.svg"),
    ("el.product.demo_fixtures", "retrieval/frozen_snapshot_phase0.json"),
    ("el.product.demo_fixtures", "markets/golden_market_structures.json"),
    ("el.product.demo_fixtures", "hormuz_rules.json"),
    ("el.product.demo_fixtures", "hormuz_impact_copy.json"),
    ("migrations", "env.py"),
    ("migrations", "versions/4f7c2e9a6b13_anonymous_demo_sessions.py"),
)


def _child_environment(db_url: str) -> dict[str, str]:
    # An explicit environment allowlist prevents inherited provider credentials,
    # retrieval configuration, or a PYTHONPATH pointing back into the checkout.
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT")
           if name in os.environ}
    env.update({"FITCHECK_UI_MODE": "fixture", "FITCHECK_ANONYMOUS_DEMO": "1",
                "FITCHECK_UI_DB_URL": db_url, "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1", "OTEL_SDK_DISABLED": "true"})
    return env


def _stop(children: list[subprocess.Popen]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("fitcheck-demo.sqlite"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)
    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535")
    db_path = args.db.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for package, relative in REQUIRED_RESOURCES:
        resource = files(package).joinpath(*relative.split("/"))
        if not resource.is_file():
            raise SystemExit(f"demo resource missing: {package}/{relative}")
    db_url = f"sqlite:///{db_path}"
    # Migrate once before starting two independent processes. An existing
    # unversioned or stale database is rejected by make_session_factory.
    factory = make_session_factory(db_url)
    factory.kw["bind"].dispose()
    env = _child_environment(db_url)
    children: list[subprocess.Popen] = []
    stopping = False

    def on_signal(_signum, _frame):
        nonlocal stopping
        stopping = True

    prior_int = signal.signal(signal.SIGINT, on_signal)
    prior_term = signal.signal(signal.SIGTERM, on_signal)
    try:
        children.append(subprocess.Popen(
            [sys.executable, "-m", "el.sourceinterpretation.worker", "--poll-seconds", "0.3"],
            env=env,
        ))
        children.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "el.product.app:app",
             "--host", args.host, "--port", str(args.port)],
            env=env,
        ))
        print(f"FitCheck fixture demo: http://{args.host}:{args.port}/", flush=True)
        while not stopping:
            for label, child in zip(("source worker", "web"), children):
                code = child.poll()
                if code is not None:
                    print(f"{label} exited with status {code}; stopping demo", file=sys.stderr)
                    return 1
            time.sleep(0.2)
        return 0
    finally:
        _stop(children)
        signal.signal(signal.SIGINT, prior_int)
        signal.signal(signal.SIGTERM, prior_term)


if __name__ == "__main__":
    raise SystemExit(main())
