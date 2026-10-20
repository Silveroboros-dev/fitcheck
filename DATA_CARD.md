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
- newly written minimal synthetic candidate-contract abstractions;
- expected market structures and fit-policy cases; and
- small synthetic recall and schema sentinels used by offline tests.

The original public copy includes four authored thesis proposals, a synthetic
market snapshot, paired authored market structures, and synthetic recall
sentinels. Identifiers and URLs are fabricated or neutralized. Values may be
intentionally stale or simplified so a test remains deterministic.

The anonymous demo uses one fixed Stablecoin/Robinhood source with two
candidate theses in source order. Each has a clarification envelope and a
ready revised proposal, which still requires an explicit visitor acceptance.
After explicit visitor acceptance, each exact supported thesis compares three
frozen minimal synthetic test abstractions. The Stablecoin pool contains
`synthetic-stablecoin-total-cap`, `synthetic-stablecoin-unit-cap`, and
`synthetic-treasury-ledger-payment`. The Robinhood pool contains
`synthetic-broker-tokenized-asset`, `synthetic-broker-first-tokenized`, and
`synthetic-broker-chain-token`. Two synthetic event bindings,
`synthetic-event-stablecoin-cap` and `synthetic-event-tokenized-listing`, bind
only the unit-cap and first-tokenized children, respectively. Their
`fixture_versioned_utc` values version deterministic fixture content; they are
not capture times or evidence of a provider read. Four curated proposals are
`weak_proxy` and two are `not_an_expression`, separate from the unchanged
automatic diagnostics. These proposals are not named human adjudications,
goldens, current prices, or catalog-wide conclusions. Complete fixture
resolution rules remain accessible.
The packaged Phase-0 snapshot remains a regression resource for other fixture
and MCP paths, not a substitute pool in this journey.

The third scenario, `el/product/demo_fixtures/agi2028.json`, is a
project-authored reconstruction of a saved three-contract comparison. It
compares a public autonomous-AI-research demonstration by the end of 2028 with
recursive self-improvement, a Millennium Prize solution announcement, and an
OpenAI AGI announcement. Its titles and condition summaries are newly written
paraphrases of real contract topics. They are not synthetic market claims,
verbatim provider rules, provider responses, or live prices. The content date
versions this authored case; it is not a retrieval timestamp.

Both views retain `no_clean_expression` over exactly three contracts. The
earlier-label view reconstructs three `not_an_expression` results. The current
view presents RSI as a `weak_proxy` relatedness proposal and keeps the other
two labels unchanged. Neither view is a fresh inference, named human
adjudication, golden, or exact replay of the private run. The private original
and its human-decision and rule-capture lineage are excluded. This read-only
case creates no thesis acceptance or market-selection record.

The separate Hormuz resource is the locked
`el/product/demo_fixtures/hormuz_rules.json` projection of retained historical
Polymarket rules for market `2176270`: exact old and new rule text, observed
timestamps and hashes. Its complete-file SHA-256 is
`56c45b0e41fdb0d1dd94654aff12fde49a8fc18fc71b168da148c4d84678da2f`;
the embedded old/new rule hashes are respectively
`967727828ff22a05e4807ef3728b95349fb007ce0d332804e3af45e27163da8b`
and `cb371ec8e828421f47cbb688988125d12401981984ee047e317fc25d02bc0b07`.
The separate project-authored `hormuz_impact_copy.json` supplies the narrowly
bound possible-effects explanation. Neither resource contains a copied
database row, private QA identifier, raw capture archive, or historical human
decision. The demo reconstructs thesis acceptance and market choice per
visitor as labelled test setup, with semantic market fit explicitly
unassessed.

## Provenance and license

The repository owner confirmed on 2026-08-10 that the original selected
fixture set was project-authored synthetic material. The retained Hormuz rule
wording is historical market content, identified as such rather than claimed
as project-authored synthetic text. On 2026-10-16, the owner directed that the
exact locked projection remain the sole external-content exception and
reconfirmed the narrowly bound possible-effects copy for this fixture demo.
The owner subsequently adopted `hormuz-publication-risk-exception-v1` on the
same date, explicitly accepting the residual publication risk and assessing
it as remote despite the absence of a documented redistribution basis.
The disposition is `rights_verified=false`,
`third_party_permission=unproven`, and `owner_risk_accepted=true`.
This satisfies release gate 1 only through the exact-content owner exception
in `PUBLIC_REPO_BOUNDARY.md`; it is not a finding of verified rights.
Neither decision asserts project authorship, third-party permission, or a
legal conclusion. The six market
fixtures and two event bindings are newly written minimal synthetic test
abstractions that preserve only the required demo edge cases. They use
FitCheck-owned schemas and are not provider response bodies or provider
schemas. Their identifiers, event relationships, URLs, timestamps, and
liquidity values are fabricated. No included resource is a hydrated social
post or governed review row. The code and newly written minimal synthetic
fixture abstractions are distributed under Apache License 2.0; see `LICENSE`.
The newly authored AGI 2028 summaries and explanatory copy are also project
text under that license; they make no claim to ownership of the underlying
market facts, company names, or third-party rules.
The historical title and rule wording are outside that grant; see
`THIRD_PARTY_NOTICES.md`.

## Excluded data

The public fixture set deliberately excludes:

- the governed human-review corpus and its source packets;
- reviewer identities, free-form notes, and local provenance;
- hydrated social-platform text or other redistributed source text;
- external-provider market or event responses, and credentials;
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

Every proposed public fixture must have a documented purpose and origin. New
market and event fixtures must be newly written minimal synthetic test
abstractions, or an explicitly enumerated project-authored educational summary
such as the AGI 2028 case above. An authored summary must identify real topics
honestly and must not copy provider rules or private evidence records.
The six market fixtures and two event bindings above do not rely
on a provider-body redistribution exception. The retained Hormuz rules
projection remains the sole declared external-content exception; any byte
change, broader use, or addition needs a separate scope and rights review.
Fixtures must contain no credentials, personal data, private reviewer
material, undeclared third-party payload, live service identifier, or local filesystem
path. Prefer fabricated entities, `example.invalid` URLs, and deterministic
timestamps. Changes must pass the public boundary checker and the relevant
offline tests.
