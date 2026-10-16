# Fit gate policy contracts v1

Status: active public implementation contract.

This document groups the public compatibility and safety rules covered by the
FC-R01, FC-R02, FC-R03, FC-R04, and FC-Q01 regressions. These identifiers are
retained so a public change can be traced to the same executable contract as
the private source without exposing private review data.

## Generated fit text (FC-R01)

Retained source text and source identifiers are admissible evidence, but they
are not trusted generated copy. Deterministic check details, recommendation
reasons, and FitCard explanations pass through the same safe formatter in the
synchronous service and asynchronous classification worker. Restricted source
wording is replaced with a neutral description in generated fields. Exact
source evidence remains attributable in provenance under the quote policy
below.

## Versioned token rules (FC-R02)

Historical `tok-v1` evidence replays unchanged. The stopword-corrected
successor is an explicit policy selection with its own gate and token-rule
versions. Every per-market verdict and persisted FitCard records the selected
versions. Mixed, forged, or unknown version pins fail closed.

The detailed token behavior and replay rules are specified in
[`fitcheck-token-rules-v2-policy.md`](fitcheck-token-rules-v2-policy.md).

## Advisory veto provenance (FC-R03)

An accepted advisory may only demote a deterministic result. Each retained
veto records its condition, cap, severity, and verified evidence. Before the
advisory can affect a verdict, the service recomputes hashes from the retained
capture bytes and validates the complete source binding. A mismatch causes a
deterministic fallback; a later output check is an additional boundary rather
than the first provenance check.

## Immutable runtime policy (FC-R04)

Service construction defensively copies and deeply freezes all policy data,
including composite claim and market coverage. Caller mutation after service
construction cannot change a verdict under an existing policy identity.
Runtime policies retain their public JSON wire shape and support stable
serialization round trips.

## Exact attributed source quotes (FC-Q01)

The successor quote policy allows restricted vocabulary only in a literal,
attributed source quote. Generated advice remains prohibited. A quote must be
an exact substring of one individual source field; concatenating contract
terms with resolution rules cannot create a valid quote.

The source binding covers the thesis, candidate set and member, market,
snapshot, rules capture, hashes, and token-rule version. The output boundary
revalidates those identities, current retained bytes, and field-local spans.
Cards lacking the complete successor binding receive no quote exemption.

The detailed field policy is specified in
[`fitcheck-advisory-source-quotes-policy-v2.md`](fitcheck-advisory-source-quotes-policy-v2.md).

## Public verification

The public export carries synthetic, credential-free regressions for generated
text, token-version replay, advisory veto provenance, deep policy immutability,
and exact quote binding. It also carries end-to-end classification worker
coverage. These tests establish deterministic contract behavior over their
fixtures; they do not establish live-provider accuracy or authorize generated
financial advice.
