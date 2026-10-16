# Minimal synthetic frozen market demo contract v3

Status: active local offline implementation and public-distribution contract.
This succeeds `fitcheck-frozen-market-demo-contract-v2.md` for every current
demo pool. Product Discovery v3.1 and the v3 market-pool contract remain
authoritative. It first ships in the FitCheck `0.2.0` public package; v1/v2
frozen-demo runtime compatibility is explicitly unsupported.

## Scope and authority

The exact accepted Stablecoin/Treasury thesis may show these three fixtures:

1. `synthetic-stablecoin-total-cap` from
   `synthetic-market-stablecoin-total-cap.json`;
2. `synthetic-stablecoin-unit-cap` from
   `synthetic-market-stablecoin-unit-cap.json`; and
3. `synthetic-treasury-ledger-payment` from
   `synthetic-market-treasury-ledger-payment.json`.

The exact accepted Robinhood thesis may show these three fixtures:

1. `synthetic-broker-tokenized-asset` from
   `synthetic-market-broker-tokenized-asset.json`;
2. `synthetic-broker-first-tokenized` from
   `synthetic-market-broker-first-tokenized.json`; and
3. `synthetic-broker-chain-token` from
   `synthetic-market-broker-chain-token.json`.

These are fixed, curated candidate pools. They are not a catalog search, a
claim about real markets, or a statement of all available expressions. No
other accepted structure may borrow them. Curated classes are
`offline_demo_proposal` under `frozen-demo-curation-v3`: the total-cap,
unit-cap, tokenized-asset, and first-tokenized fixtures are `weak_proxy`; the
Treasury-payment and chain-token fixtures are `not_an_expression`. This
authority is not a visitor choice, named human adjudication, or golden. The
ordinary FitCard, checks, confidence, and trace remain separate automatic
diagnostics.

## Synthetic provenance boundary

All six market resources use schema
`fitcheck.synthetic_market_fixture.v1`. The two event resources use schema
`fitcheck.synthetic_event_fixture.v1`:

- `synthetic-event-stablecoin-cap` in
  `synthetic-event-stablecoin-cap.json` binds only
  `synthetic-stablecoin-unit-cap`; and
- `synthetic-event-tokenized-listing` in
  `synthetic-event-tokenized-listing.json` binds only
  `synthetic-broker-first-tokenized`.

The eight resources are newly written minimal synthetic test abstractions that
preserve only the required demo edge cases. They use FitCheck-owned schemas and
are not provider response bodies or provider schemas. Their market, event, and
condition identifiers, event relationships, URLs, version timestamps, and
liquidity values are fabricated for deterministic tests. URLs use the reserved
`example.invalid` host. `fixture_versioned_utc` versions fixture content; it
does not claim an external observation or capture. The resources contain no
provider endpoint, raw response, HTTP capture claim, CLOB token, live price, or
final outcome.

The reserved source URL remains provenance metadata and is never presented as
an outbound destination. The fixture UI links to the exact packaged market
resource through an allowlisted same-origin route.

These resources do not rely on the former provider-response exception for
public distribution. This does not change the separately declared, narrowly
scoped Hormuz retained-rules exception.

## Binding and evidence

The v3 provider identity is `frozen-demo-six-market-v3`; eligibility uses
`frozen-demo-six-market-eligibility-v3`. Each card binds the accepted
`thesis_analysis_id` and full accepted-structure SHA-256 to one
`candidate_set_id`, `snapshot_id`, synthetic `market_id`, `condition_id`,
fixture SHA-256, full-rule SHA-256, `rules_capture_id`, `fit_card_id`, and
`market_assessment_id`. Evidence uses schema
`fitcheck.frozen_demo_evidence.v3` and records the resource name, byte count,
fixture digest, `fixture_versioned_utc`, and
`fixture_metadata_authority=project_authored_synthetic`.

An event association is present only for the two declared child fixtures. It
must match exactly one embedded child by both `market_id` and `condition_id`,
and every embedded market field must equal the standalone market fixture.
Cross-bound, duplicate, partial, or altered event data fails closed. The other
four markets have no event association.

All cards expose their own title, proposal class, capture and miss reasons,
Yes/No summary, rule deadline and source, complete fixture rules, and synthetic
fixture provenance. No card presents a fixture timestamp, liquidity value,
price, or outcome as current real-world evidence.

Three synthetic rule details are binding regression semantics. The
`synthetic-treasury-ledger-payment` fixture retains both its primary
transaction-deadline clause and its distinct supplemental
announcement-deadline clause, including the exclusions for pilots, tests, and
proofs of concept and the inclusion of payments by the Treasury or an
authorized Treasury sub-body or entity. The
`synthetic-broker-first-tokenized` fixture retains its first-winner condition
and same-ET-calendar-day tie-break: earlier purchasability wins, followed by
earlier public confirmation when that order cannot be established. The
`synthetic-broker-chain-token` fixture requires two checks: a post-version
designation and new launch as the network's operating or voting asset, and
actual public exchange in a functioning market. A promotional statement or
roadmap is insufficient. Pegged-value assets, joke or community assets,
staking-receipt assets, derivative or synthetic claims, and representations of
shares or funds remain outside this test case. Either unmet check produces No.

The display policy ranks the three independently assessed pairs.
`no_clean_expression` means only that the exact non-empty displayed pool of
three has three terminal weak or negative assessments. Missing, partial,
cross-bound, or tampered evidence is incomplete or unavailable, never
no-clean. A visitor begins with `unanswered`; explicit `none` and one chosen
displayed market are separate persisted states. Source or thesis changes and
late responses clear stale pool and choice state.

## Historical compatibility

V1 and v2 remain private historical evidence contracts. Their provider-backed
resources, numeric market identities, saved pools, and capture claims are not
inputs to the active v3 provider or the public distribution. Active runtime
paths reject persisted v1 or v2 frozen-demo evidence rather than relabelling,
reprojecting, or reading it through v3. Any future private historical reader or
migration requires its own explicit contract and cannot reintroduce those
resources into a public artifact.

## Acceptance criteria

- **AC-S1:** Each exact accepted thesis receives exactly its three named
  synthetic markets; unsupported or altered theses fail closed.
- **AC-S2:** The six market files validate as
  `fitcheck.synthetic_market_fixture.v1`, and the two event files validate as
  `fitcheck.synthetic_event_fixture.v1`; no active resource uses a provider
  response schema.
- **AC-S3:** Every card carries complete thesis, market, condition, snapshot,
  rules, candidate, assessment, FitCard, resource, and digest identity;
  cross-binding or byte changes fail closed.
- **AC-S4:** The two declared event bindings each match one exact standalone
  child by ID, condition, and field equality. The other four fixtures have no
  inferred event association.
- **AC-S5:** Each card exposes complete fixture rules, its declared
  deadline and source basis, and honest synthetic provenance. No provider
  capture, current price, or final outcome is asserted. Reserved `.invalid`
  provenance is not an outbound UI link; the inspectable source control serves
  only the bound packaged market resource from the same origin. The
  Treasury-payment fixture preserves both distinct deadline clauses and
  pilot/test/proof-of-concept exclusions while retaining authorized Treasury
  sub-body/entity scope; the first-tokenized fixture preserves first-winner and same-ET-day
  purchasability/public-confirmation tie-break semantics; and the chain-token
  fixture requires both post-version launch/designation and functioning public
  exchange, rejects promotional statements alone, and excludes the declared
  pegged-value, community, staking-receipt, synthetic/derivative, and
  equity-representation categories.
- **AC-S6:** The four `weak_proxy` and two `not_an_expression` proposals remain
  separate from unchanged automatic FitCard diagnostics and never claim human
  authority.
- **AC-S7:** Only the exact, non-empty, fully assessed three-card displayed
  pool may receive no-clean; partial assessment stays incomplete.
- **AC-S8:** Unanswered, explicit none, and any one displayed-market choice
  remain distinct, bound, and idempotent. Reset, source or thesis switch, late
  responses, and mismatched panels clear or reject stale state.
- **AC-S9:** Active runtime paths reject v1/v2 provider-backed frozen-demo
  evidence and never reproject it as v3 synthetic evidence.
- **AC-S10:** The public artifact contains the eight declared synthetic
  resources and no retired provider market or event body, endpoint, payload
  field, capture metadata, or numeric provider identity from v1/v2.
- **AC-S11:** Frozen replay, package installation, tests, and the fixture UI
  require no provider/model call or credential and cannot mutate review
  decisions or goldens.
- **AC-S12:** `DATA_CARD.md`, `README.md`, `AGENTS.md`, and
  `PUBLIC_REPO_BOUNDARY.md` consistently identify the fixtures as
  minimal synthetic test abstractions while preserving the separate Hormuz
  exception.

Verification uses focused fixture, API, DOM, package-resource, export-boundary,
and offline regression tests plus browser replay. No live service, provider,
model, database export, or deployment evidence is required by this contract.
