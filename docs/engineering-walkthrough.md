# FitCheck engineering walkthrough

This walkthrough explains the engineering decisions represented in the public
FitCheck distribution. It distinguishes inspectable implementation and tests
from the private reference deployment and from integrations that are
deliberately excluded.

The product-flow description below documents the predecessor reference and
its retained backend foundations. The current default UI instead follows the
[v3.1 human-confirmed source-to-market journey](fitcheck-v3.1-ui-contract.md);
the [README](../README.md) documents its runnable fixture UI and separate MCP
source worker. The blind-prior and simple-ledger routes remain compatibility
surfaces. This walkthrough is not evidence of current deployment state or
semantic accuracy.

## Problem

Finding a prediction market that mentions the same entity or topic as a thesis
is not enough. A useful expression must also align on event identity, stage,
direction, threshold, time horizon, and resolution conditions. A semantically
near contract can still be a weak proxy or resolve on a materially different
event.

That creates two linked engineering problems:

1. Retrieval must preserve enough source and snapshot provenance for a later
   judgment to be inspected.
2. A probabilistic model may help structure text, but it must not silently own
   the final label, evaluation truth, or promotion decision.

FitCheck treats `no_clean_expression` as a valid outcome rather than forcing a
candidate into a recommendation-shaped result.

## System

The predecessor local product loop accepts an event-risk thesis, records a blind prior,
retrieves candidates from a frozen synthetic snapshot, applies structure and
fit gates, produces a fit card, and can save a ledger entry. A disagreement
with a card creates a review candidate; it does not rewrite the card or a
governed label.

The worker foundation separates expensive or failure-prone retrieval from an
interactive request:

1. A caller submits an idempotent job with semantic input, ownership,
   correlation, retry, availability, and deadline metadata.
2. A worker claims a leased attempt. The attempt identity fences later domain
   writes, so an expired worker cannot commit after another worker takes over.
3. A snapshot source stages normalized rows behind a protocol. The public
   distribution supplies fixtures, not a live provider source.
4. Validation computes content and membership identity, checks declared
   sentinels and policy, and publishes an artifact only when the contract
   passes.
5. Promotion records the immutable snapshot and advances a separately locked
   active pointer. Older snapshots are superseded; same-cutoff content drift
   requires operator attention.

Classification uses pinned policy and model-adapter identities. Candidate
eligibility and the final expression class are deterministic. Model-produced
structures remain proposals with recorded provenance.

## Engineering scope

The engineering work represented in this distribution includes:

- translating a vague "find a related market" request into explicit domain
  structures, expression-quality classes, and a valid no-match outcome;
- implementing a transport-independent job state machine with idempotent
  submission, bounded retry, deadlines, cancellation, leases, reaping, and
  fenced writes;
- designing content-addressed snapshot validation and transactional promotion
  so retrieval evidence cannot change silently underneath a judgment;
- separating model proposals from deterministic fit policy and human-owned
  review truth;
- implementing correction intake that is idempotent and candidate-only rather
  than a direct mutation of evaluated artifacts;
- exposing the same core services through a local product API and MCP-oriented
  contracts without moving policy into a transport layer; and
- building a manifest-only public export with an immutable lock, content
  scans, synthetic fixtures, CI, package construction, and a non-root fixture
  container.

These are code and contract claims, linked below. They are not a claim of
institutional adoption, live-provider authorization, or independently audited
production reliability.

## Tradeoffs

### Deterministic policy over model-owned classification

Deterministic checks are easier to inspect, version, and regression-test, and
they put a ceiling on certain false-positive expression claims. The cost is
coverage: rules and aliases require governed evidence, review, and deliberate
promotion as new contract language appears.

### Frozen snapshots over direct live reads

Frozen evidence makes a fit decision replayable and preserves the exact rules
the system judged. It is less fresh than reading a provider inside every
request, so refresh is a separate job and active-snapshot promotion is
explicit.

### Provider-neutral workers over a cloud-specific queue

The public state machine and worker contracts do not depend on Cloud Tasks,
HTTP, MCP, or a particular provider SDK. That makes failure semantics
offline-testable and preserves deployment choice. The MCP boundary separately
uses SQL-backed per-key weighted admission across instances. Queue wiring,
provider authorization and provider-specific throttling, retention policy, and
currency-denominated cost enforcement remain operational work, not
capabilities implied by this repository.

### Review candidates over direct learning

Candidate-only corrections prevent one user action or model run from changing
golden truth. The cost is latency and operating effort: a human must review and
promote changes through governed policy.

### Curated export over a public canonical repository

A fresh allowlisted distribution keeps governed rows, provider payloads,
deployment identifiers, and private collaboration history outside the public
Git boundary. The tradeoff is a deliberate contribution workflow: public
changes are reviewed and ported into the private source, then exported again.

## Failure modes

| Failure | System response | Residual limit |
| --- | --- | --- |
| Duplicate submission or delivery | Semantic input and execution guarantees are bound to an idempotency key; conflicting reuse is rejected. | Idempotency depends on callers supplying stable logical keys. |
| Worker crashes or loses its lease | Attempts expire and can be reaped; transaction fences prevent a stale attempt from owning a later write. | External side effects must independently honor idempotency; the job ledger cannot undo an arbitrary provider action. |
| Transient provider or storage error | The worker records a typed failure and schedules bounded retry with backoff. | Persistent failure ends as failed, retry-exhausted, or operator-required; retries do not manufacture availability. |
| Malformed, incomplete, or suspicious snapshot | Validation rejects the staged artifact before active promotion. | Public fixtures do not establish that every live provider anomaly is covered. |
| Same cutoff produces different content | Promotion refuses the restatement and requires operator review. | An operator still needs source-side evidence to decide whether the change is legitimate. |
| Model proposes an invalid or weak structure | Structure and fit gates can refuse, cap, or return `no_clean_expression`. | Deterministic rules can under-call novel but valid expressions and need reviewed-data growth. |
| User disputes a fit card | A pending review candidate is minted; existing cards and ledger truth remain unchanged. | Resolution is intentionally not automatic and requires a review process. |
| Public export drifts across the private boundary | Manifest, file-set, path, identifier, secret, and immutable-lock checks fail closed. | Automated scanning supplements rather than replaces human review of the generated tree. |

## Evidence

The public repository provides several evidence levels. They should not be
collapsed into one broad "production-ready" claim.

### Implemented and tested in public

- Job lifecycle and fenced writes:
  [`el/jobs/store.py`](../el/jobs/store.py),
  [`tests/test_job_store.py`](../tests/test_job_store.py), and
  [`tests/test_job_store_postgres.py`](../tests/test_job_store_postgres.py).
- Snapshot staging, validation, artifact identity, and promotion:
  [`el/retrieval/snapshot_worker.py`](../el/retrieval/snapshot_worker.py) and
  [`tests/test_snapshot_worker.py`](../tests/test_snapshot_worker.py).
- Pinned classification execution and deterministic final authority:
  [`el/classification/worker.py`](../el/classification/worker.py),
  [`el/classification/planner.py`](../el/classification/planner.py), and
  [`tests/test_classification_worker.py`](../tests/test_classification_worker.py).
- Deterministic fit policy:
  [`el/fitgate/`](../el/fitgate/) with
  [`tests/test_fit_service.py`](../tests/test_fit_service.py) and
  [`tests/test_fitgate_checks.py`](../tests/test_fitgate_checks.py).
- Candidate-only correction behavior:
  [`tests/test_correction_loop.py`](../tests/test_correction_loop.py).
- Public-boundary enforcement:
  [`scripts/build_public_export.py`](../scripts/build_public_export.py),
  [`scripts/check_public_boundary.py`](../scripts/check_public_boundary.py),
  and [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

### Runnable public demonstration

The fixture UI runs locally with synthetic authored data and SQLite. It
demonstrates the product interaction and deterministic contracts without a
provider account, cloud account, or model credential. It is not a production
authentication, deployment, or multi-tenant reference.

### Private operational statement

A reference service is operated separately on an IAM-private Cloud Run
surface with Vertex AI and Cloud SQL behind a dedicated runtime identity. Its
identifiers, deployment configuration, secrets, database, and operational
evidence are intentionally excluded, so the public repository does not
independently reproduce or attest that runtime.

### Not evidenced by the public artifact

- live-provider authorization, throughput, recall, rate-limit behavior, or
  data rights;
- real-world fit accuracy, calibration, or market coverage;
- production-scale load, availability, incident response, or tenancy; and
- trading, custody, brokerage execution, or financial performance.

See the [public fixture data card](../DATA_CARD.md),
[repository boundary](../PUBLIC_REPO_BOUNDARY.md), and
[security policy](../SECURITY.md) for the corresponding evidence limits.
