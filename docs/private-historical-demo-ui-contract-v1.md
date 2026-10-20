# Optional private historical demo, v1

Status: local optional replay. The later packaged AGI reconstruction is governed
by `agi2028-curated-demo-ui-contract-v1.md`. An explicitly configured private
replay takes precedence over that reconstruction without publishing its data.

## Evidence and authority

The configured file is a sealed historical presentation, not a golden or a
current visitor's thesis. It contains the original input, an attributable
historical human acceptance, and the exact saved comparison pool. The original
source-interpretation lineage may be unavailable. Input provenance must not be
fabricated to fill that gap. Labels retain their recorded authority.

The loader verifies identity and integrity of the operator-pinned presentation.
It does not independently reclassify a pair or prove the runtime projection's
stacking-only predicate from UI cards. Those checks belong to the assessment
producer and its retained diagnostics. A sealed packet is not a semantic
adjudication or a policy-execution certificate.

The immutable binding tuple is case ID, canonical payload SHA-256, thesis ID,
normalization decision ID, display-set ID, candidate-set ID, fit-card ID and
snapshot ID. Candidate assessment IDs, membership IDs and rule-capture IDs are
unique within that pool. Render in saved display-rank order.

## Acceptance criteria

- AC-H1: The exact private replay is absent by default. An explicit private file enables
  it only in anonymous fixture mode. The launcher requires a loopback host and
  an expected SHA-256 pinned separately from the file. The app also rejects
  non-loopback clients and non-local Host headers when private history is enabled.
  No private case is packaged, exported, or served as a static asset.
- AC-H2: Startup verifies the externally pinned digest, typed response schemas and the
  complete cross-panel binding. Missing, tampered, incomplete, mixed-snapshot,
  mixed-thesis or contradictory pool data fails closed.
- AC-H3: A fixed GET route returns this configured historical case, independent
  of visitor ownership. It does not import historical rows, accept a thesis,
  select a market, adjudicate semantics, retrieve markets or call a model.
  Existing owner-scoped APIs retain their ownership checks.
- AC-H4: The menu and reloadable `demo_history` link display the recorded date,
  original input and accepted public criterion, full contract rules, saved
  comparison and provenance. The date is the analysis date, not resolution of
  the future thesis. No current prices, monitoring or causal effects are claimed.
- AC-H5: All historical panels bind before any are rendered. Acceptance is
  explicitly attributed to the original session, not the visitor. Source,
  discovery, market selection and feedback actions remain inactive. Unanswered
  market choice stays unanswered; viewing a no-clean pool does not record none.
- AC-H6: Journey changes clear prior panels, selections and resume parameters.
  Only one menu reset is in flight; stale case/session/token responses cannot
  replace newer state. A failed or unavailable history is visible, with no
  fallback to a live run. The other journeys remain usable.
- AC-H7: Verify schemas, optional absence, tamper/mismatch rejection, GET
  immutability, owner isolation, stale response suppression and journey switching
  with offline tests. Verify the served page, all three cards, reload and the
  other journeys in a real browser. Preserve the original database and captures.

No canonical truth is mutable on this surface. The only writes during journey
selection are ordinary disposable visitor-session creation/reset bookkeeping.
The packaged interactive journeys retain their existing fixture-only writes.

Local launch uses the existing fixture launcher:

```sh
python -m el.product.demo_launcher --host 127.0.0.1 \
  --db /path/to/disposable-demo.sqlite \
  --historical-case /path/to/private-case.json \
  --historical-case-sha256 EXPECTED_CANONICAL_PAYLOAD_SHA256
```

The file is an envelope with `case` and `sha256`. The expected digest is pinned
in the operator's launch command separately. Neither path nor private case
contents belong in packaged fixtures or repository documentation.
