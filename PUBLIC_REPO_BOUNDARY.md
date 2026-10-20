# Public repository boundary

## Purpose

This repository is a curated public distribution, not a mirror of the private
canonical development repository. Its purpose is to make core FitCheck code,
safe documentation, synthetic fixtures, and reproducible tests inspectable
without exposing governed evidence, provider material, deployment state, or
private collaboration history.

The public distribution must be generated into a fresh directory from an
explicit allowlist. Do not publish the canonical repository by changing its
visibility, and do not import its existing Git history into the public
repository.

## Allowed artifact classes

- Core `el/` Python packages after automated boundary checks.
- Selected deterministic tests and newly written minimal synthetic fixtures.
- The explicitly enumerated `el/product/demo_fixtures/agi2028.json`
  educational case, containing project-authored summaries of real contract
  topics and explanatory labels. This is original explanatory text, not
  another exception for redistributing provider text. No raw rules, provider
  response, live price, private human decision or capture record is included.
  Its reconstruction and proposal status must remain visible.
- The exact locked `el/product/demo_fixtures/hormuz_rules.json` projection
  declared in `DATA_CARD.md` and `THIRD_PARTY_NOTICES.md`, solely for the
  versioned retained-rules fixture. File SHA-256
  `56c45b0e41fdb0d1dd94654aff12fde49a8fc18fc71b168da148c4d84678da2f`
  is the one owner-directed external-content exception. It covers no live
  refresh, raw response, price or outcome history, other market, or future byte
  change. The historical wording is not claimed as project-authored text and is
  outside the Apache License 2.0 grant. Its publication is governed by the
  bounded owner risk exception below; redistribution rights remain unverified.
  Any content change, addition, or broader use requires a new owner decision
  and scope, rights, and boundary review.
- Public architecture and experiment summaries that contain no restricted
  source rows or operational identifiers.
- Public package metadata, CI, container files, and contributor-facing policy
  documents.
- Generic examples that use reserved domains, fabricated identifiers, and
  placeholder configuration names.

Allowlisting a directory does not allow every future file in that directory.
The export manifest must enumerate files or narrowly defined patterns, and the
generated tree must reject unexpected additions.

## Always private or excluded

- Governed review roots, review packets, judgments, candidate batches,
  promotion inputs, generated exports, archives, and evaluation outputs.
- Raw source text, hydrated social content, and external-provider payloads
  beyond the exact locked Hormuz projection declared above; provider-specific
  private clients, vendored wheels, and credentials.
- Environment files, API keys, database URLs, service-account material,
  cookies, tokens, runtime databases, logs, and traces.
- Live cloud project, service, account, database, secret, bucket, or endpoint
  identifiers.
- Deployment proof, operational configuration, infrastructure state, and
  incident artifacts.
- Private handoffs, agent configuration, partner or collaborator notes,
  personal contact details, absolute local paths, and editor/runtime state.
- Symlinks, submodules, nested repositories, caches, compiled files, and
  temporary artifacts.

## Cloud and provider separation

Public source does not imply public runtime access. Historical project records
describe a predecessor FitCheck MCP deployment, but this distribution neither
provides nor claims a current live reference service. Any future reference
deployment must separately manage secrets, identity, database, quotas, and
deployment configuration. A future public demo must use a distinct,
synthetic-data environment with its own abuse controls and budget limits.

The public retrieval provider interface is an extension boundary, not a claim
that a live provider implementation or its data is redistributable. The
published adapter is fixture-only and live-provider selection fails closed.
Anyone adding a separate integration is responsible for authorization, terms,
rate limits, credentials, retries, and data-retention rules.

## Required release gates

Publication is blocked unless all of the following are true:

1. The owner has selected a code license, added `LICENSE`, verified artifact
   rights, and recorded fixture provenance in `DATA_CARD.md`, with the sole
   rights-verification exception defined below for the exact locked Hormuz
   projection. That exception satisfies this gate by explicit owner risk
   acceptance, not by a finding that redistribution rights are verified.
2. The export was built from the declared allowlist into a clean directory.
3. Boundary scans find no secret, personal path, private name, live identifier,
   prohibited data root, symlink, nested repository, or unexpected file.
4. The complete exported offline test suite passes without network access or
   credentials.
5. The package and fixture Docker image build from the exported tree.
6. A reviewer examines the generated file list and representative fixture
   contents, not only the exporter exit code.
7. The destination repository has private vulnerability reporting, secret
   scanning, push protection, dependency alerts, code scanning, and protected
   default-branch checks enabled.
8. If release materials claim or link a live reference service, an independent
   read-only check has verified IAM-private ingress; no public-repository check
   is accepted as proof of cloud state. When release materials make only a
   clearly historical deployment statement and claim no current service, this
   gate is recorded as not applicable.

## Hormuz publication risk exception v1

Policy ID: `hormuz-publication-risk-exception-v1`. Adopted by the repository
owner on 2026-10-16, supplementing the earlier decision to retain the exact
projection. The owner explicitly accepted the residual publication risk and
assessed it as remote despite the absence of a documented redistribution
basis. This is the owner's risk assessment, not a legal conclusion.

Decision origin and evidence: the development assistant recorded this policy
from the owner's explicit instruction in the maintainer session on
2026-10-16. This is an agent relay of that decision, not an independent human
attestation. The private canonical approval record for this policy ID has
SHA-256 `f1f2031005287a70554541beb7457cb6bec39b955971b903aa6380a0087256cb`.
That record is excluded from the public export. The digest binds its retained
bytes; it does not establish redistribution rights or third-party permission.

The recorded disposition is `rights_verified=false`,
`third_party_permission=unproven`, and `owner_risk_accepted=true`.

The exception applies only to the historical title and old/new rule wording
in `el/product/demo_fixtures/hormuz_rules.json`, complete-file SHA-256
`56c45b0e41fdb0d1dd94654aff12fde49a8fc18fc71b168da148c4d84678da2f`,
with the text identities recorded in `DATA_CARD.md` and
`THIRD_PARTY_NOTICES.md`. It covers this exact projection in the public source
repository, root wheel and sdist, fixture Docker image, and offline demo.
The historical wording remains outside the Apache License 2.0 grant.

No live refresh, additional external content, changed bytes, other market, or
broader use is covered. Each requires a new owner decision and review. The
exception does not waive any other release gate, including gate 7, and does
not by itself authorize a push, merge, package publication, or deployment.

## Change discipline

Make changes in the private canonical repository, then regenerate and review a
new public candidate. Do not merge public-repository commits back mechanically.
If a public contribution is accepted, port it deliberately through the private
review process and re-export it. This keeps governed data and operational state
out of both Git history and pull-request metadata.
