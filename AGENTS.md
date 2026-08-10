# AGENTS.md - FitCheck public distribution

This repository is a curated, fixture-only public distribution. It is not a
mirror of the private canonical repository and does not contain governed
review data, live-provider adapters, deployment state, or private history.

## Read first

1. `README.md` for the runnable public surface.
2. `DATA_CARD.md` for fixture scope and limitations.
3. `PUBLIC_REPO_BOUNDARY.md` for prohibited artifacts and release gates.
4. `SECURITY.md` for vulnerability reporting.
5. The selected contracts under `docs/` when changing those interfaces.

## Architecture invariants

- Models may propose; deterministic checks and human review govern durable truth.
- Frozen fixtures are offline test truth. Live evidence must never rewrite labels.
- `no_clean_expression` is a valid result.
- Corrections create review candidates; they do not directly mutate goldens.
- Preserve snapshot, retrieval, policy, and timestamp provenance.
- Execution, wallets, custody, brokerage, and position advice are out of scope.
- Use expression-quality language, not buy/sell recommendations.

## Public-distribution rules

- Retrieval is fixture-only. `MARKET_PROVIDER=polydata` deliberately fails closed.
- Do not add credentials, live service identifiers, raw provider payloads,
  governed rows, reviewer identities, free-form review notes, or local paths.
- New fixtures must be project-authored synthetic material with documented
  origin. Sanitized third-party material remains out of scope unless the owner
  approves a separate rights review and boundary change.
- Do not present fixture results as real-world accuracy or coverage evidence.
- Keep offline tests independent of network, cloud accounts, and model credentials.

## Verification

```bash
python3.12 scripts/check_public_boundary.py --repo-root . --self-check --destination .
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest --ignore=tests/test_job_store_postgres.py
```

Run the boundary check on a fresh checkout before creating `.venv`, test
caches, build output, or other ignored local state. The checker deliberately
rejects those artifacts if they appear in the publication tree.

Before proposing a change, inspect `git diff`, run `git diff --check`, and run
the smallest meaningful tests. Use explicit paths when staging. Publication,
deployment, live calls, dependency additions, and license changes require
separate owner authorization.
