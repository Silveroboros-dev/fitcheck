# AGENTS.md - FitCheck public distribution

This repository is a curated, fixture-only public distribution. It is not a
mirror of the private canonical repository and does not contain governed
review data, live-provider adapters, deployment state, or private history.

## Read first

1. `README.md` for the runnable public surface.
2. `DATA_CARD.md` for fixture scope and limitations.
3. `PUBLIC_REPO_BOUNDARY.md` for prohibited artifacts and release gates.
4. `SECURITY.md` for vulnerability reporting.
5. `docs/fitcheck-frozen-market-demo-contract-v3.md` for the current synthetic
   six-market journey.
6. The selected contracts under `docs/` when changing those interfaces.

## Architecture invariants

- Preserve the FitCheck logo in the top-left UI header through routine updates.
  Removing it requires a direct instruction from the product owner.
- Keep UI labels and demo presentation in English. Mark translated source text
  as a translation and retain original wording in the saved evidence.

- Models may propose. Deterministic checks enforce declared schemas, safety
  conditions, provenance, and other narrow invariants; human review governs
  semantic labels and durable decisions. Neither a model nor a deterministic
  check establishes semantic truth by itself.
- Frozen fixtures are offline test truth. Live evidence must never rewrite labels.
- `no_clean_expression` is a valid result.
- Corrections create review candidates; they do not directly mutate goldens.
- Preserve snapshot, retrieval, policy, and timestamp provenance.
- Execution, wallets, custody, brokerage, and position advice are out of scope.
- Use expression-quality language, not buy/sell recommendations.

## Public-distribution rules

- Retrieval is fixture-only. `MARKET_PROVIDER=polydata` deliberately fails closed.
- Do not add credentials, live service identifiers, undeclared raw provider
  payloads, governed rows, reviewer identities, free-form review notes, or
  local paths.
- New fixtures default to newly written minimal synthetic test abstractions
  with documented origin. The six market fixtures and two event bindings in
  the current source journey use FitCheck-owned schemas and preserve only the
  required demo edge cases. They are not provider response bodies or provider
  schemas; their IDs, URLs, timestamps, liquidity values, and relationships are
  fabricated.
  The minimal retained Hormuz rule projection remains a narrow boundary
  exception for labelled fixture workflows; additions of external content need
  a separate rights and boundary review.
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
