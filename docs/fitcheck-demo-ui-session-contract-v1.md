# Local anonymous demo UI and session contract v1

Scope: the fixture-only, two-journey local candidate. This contract does not
describe live sign-in, retrieval, or historical acceptance of a reconstructed
fixture graph.

Historical status: the identity/session and Hormuz requirements remain
current. The pending source-market statements in the Surfaces section and AC-5
are superseded by `fitcheck-frozen-market-demo-contract-v3.md`; they remain
below to preserve the contract that preceded the current synthetic three-market
fixture journey. The v2 provider-backed successor is private historical
evidence and is unsupported by the active runtime.

## Identity and authority

An opaque, expiring HTTP-only cookie resolves to one `user_id` and its
`human_ui:<user_id>` actor on every request. Browser input never supplies an
actor. The same actor binds source jobs, idempotency lookup, polls, source and
normalization decisions, market assessment and choice, and revisit records.
Reset or journey switch revokes the old cookie and creates a new actor. Old
jobs may finish in storage, but the new actor cannot fetch or attach them.
Cross-origin writes fail before an anonymous session can be created.

The browser binds all panels to one current `requestToken` plus the current
source interpretation, selected candidate, accepted thesis, display set,
choice, and revisit binding. Clearing or replacing a journey increments the
token, clears stored job/resume IDs and all visible downstream selections, and
ignores late responses. A server-side foreign or stale ID returns not found.
Unanswered, explicit `none`, and selected candidate/market remain distinct.

## Surfaces

The entry chooser has two separate cases: Stablecoin/Robinhood source
discovery and Hormuz changed-rules review. The source journey shows the frozen
source above the chosen normalized thesis; source candidates are proposals.
Its market step is explicitly unavailable pending real frozen candidate-bound
markets. No displayed pool, market/none choice, or `no_clean_expression` is
created for these two demo candidates.

Hormuz is a labelled reconstruction of baseline, thesis acceptance, and
market choice for this visitor. Exact historical rules are retained evidence;
the reconstructed human decisions are test state. Its changed clause and
approved possible-effects copy are bound to this visitor's revision,
observation, capture and packet. Dismissal, reaffirmation, and opening a local
revision draft are separate states. The draft has no accepted successor and
does not invoke a rerun. Only explicit visitor decisions mutate their owned
test graph. Neither journey changes governed goldens.

## Acceptance criteria

- AC-1: Two browsers receive different actors. Either browser cannot read or
  write the other's job, graph, market choice, revision, or revisit packet.
- AC-2: A reset, case switch, or expired cookie revokes access to earlier IDs;
  late job completion cannot restore old UI state.
- AC-3: Reusing one idempotency key across browsers creates separate jobs;
  lookup and poll remain owner-scoped.
- AC-4: Same-origin requests work; cross-origin writes fail without session
  creation, including a forged `Origin` on the first request.
- AC-5: Stablecoin and Robinhood each require explicit candidate priority,
  clarification, and acceptance. Their market step visibly reports that real
  frozen candidate-bound markets are pending; it creates no display set,
  market/none choice, or no-clean conclusion.
- AC-6: Hormuz exact before/after rules and possible-effects copy render from
  packaged resources and newly bound visitor IDs. All three actions are
  distinct; cancelling or reloading a draft creates no successor revision.
- AC-7: A locally built wheel, run outside the repository with no credential
  configuration, serves the same UI and starts a fixture worker from one
  command. Missing resources or child failures are visible.
