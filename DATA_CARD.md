# Public fixture data card

## Summary

This public distribution contains a small fixture set for deterministic tests
and the local product walkthrough. The fixtures demonstrate schema validation,
source-candidate selection, normalization confirmation, retrieval ranking,
expression-quality classification, correction intake, and legacy ledger
behavior. They are not a production corpus, representative market sample, or
statistical benchmark.

## Contents

The included `tests/fixtures/` files contain:

- synthetic event-risk theses and expected structured proposals;
- project-authored synthetic candidate-contract snapshots;
- expected market structures and fit-policy cases; and
- small synthetic recall and schema sentinels used by offline tests.

The original public copy includes four authored thesis proposals, a synthetic
market snapshot, paired authored market structures, and synthetic recall
sentinels. Identifiers and URLs are fabricated or neutralized. Values may be
intentionally stale or simplified so a test remains deterministic.

The v3.1 public reference adds one inline fixture in
`el/product/wiring.py`, created on 2026-09-09: a fictional Harbor/Orchard
source with exactly two source candidates in source order. Each candidate has
one clarification envelope and one corresponding revised ready proposal,
for two clarification and two ready normalization envelopes. A ready proposal
still requires a separate human acceptance decision. This
path reuses `tests/fixtures/retrieval/frozen_snapshot_phase0.json`; focused
tests verify a three-card displayed pool without claiming an external market
count.

## Provenance and license

The repository owner confirmed on 2026-08-10 that the original selected
fixture set was project-authored synthetic material. The 2026-09-09 inline
Harbor/Orchard addition is also project-authored fictional material and needs
its own boundary review before publication. No included fixture may be a
hydrated social post, raw external-provider response, or governed review row.
The fixtures are distributed under the repository's Apache License 2.0; see
`LICENSE`.

## Excluded data

The public fixture set deliberately excludes:

- the governed human-review corpus and its source packets;
- reviewer identities, free-form notes, and local provenance;
- hydrated social-platform text or other redistributed source text;
- raw external-provider responses and credentials;
- production database rows, logs, prompts, traces, and model responses; and
- generated exports, archives, candidate batches, and unpublished judgments.

The private governed corpus remains the source of truth for reported governed
evaluations. Public fixtures must not be presented as a substitute for it.

## Intended uses

- Reproduce the deterministic offline test suite.
- Exercise the fixture-mode product flow without network access.
- Inspect schemas, service boundaries, and policy behavior.
- Add new wholly synthetic regression cases with documented intent.

## Out-of-scope uses

- Estimating real-world accuracy, coverage, calibration, or market quality.
- Training or fine-tuning a model.
- Reconstructing private review rows or third-party source content.
- Making investment, trading, or position-sizing decisions.
- Testing a live provider or production deployment.

## Limitations

The set is small, curated, and intentionally non-representative. Synthetic
examples can prove deterministic behavior but cannot establish external
validity. A passing fixture test says that a declared contract is preserved;
it does not establish that live retrieval is complete or that a model is
correct on fresh inputs.

## Contribution gate

Every proposed public fixture must have a documented purpose and reviewable
rights. It must contain no credentials, personal data, private reviewer
material, raw third-party payload, live service identifier, or local filesystem
path. Prefer fabricated entities, `example.invalid` URLs, and deterministic
timestamps. Changes must pass the public boundary checker and the relevant
offline tests.
