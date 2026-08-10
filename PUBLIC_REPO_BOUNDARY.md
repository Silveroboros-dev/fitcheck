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
- Selected deterministic tests and project-authored synthetic fixtures.
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
- Raw source text, hydrated social content, external-provider payloads,
  provider-specific private clients, vendored wheels, and credentials.
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

Public source does not imply public runtime access. The reference deployment is
IAM-private and uses separately managed secrets, identity, database, quotas,
and deployment configuration. A future public demo must use a distinct,
synthetic-data environment with its own abuse controls and budget limits.

The public retrieval provider interface is an extension boundary, not a claim
that a live provider implementation or its data is redistributable. The
published adapter is fixture-only and live-provider selection fails closed.
Anyone adding a separate integration is responsible for authorization, terms,
rate limits, credentials, retries, and data-retention rules.

## Required release gates

Publication is blocked unless all of the following are true:

1. The owner has selected a code license, added `LICENSE`, verified artifact
   rights, and recorded fixture provenance in `DATA_CARD.md`.
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
8. The live reference service has been independently verified as IAM-private;
   no public-repository check is accepted as proof of cloud state.

## Change discipline

Make changes in the private canonical repository, then regenerate and review a
new public candidate. Do not merge public-repository commits back mechanically.
If a public contribution is accepted, port it deliberately through the private
review process and re-export it. This keeps governed data and operational state
out of both Git history and pull-request metadata.
