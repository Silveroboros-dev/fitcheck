# FitCheck v3.1 UI contract

Status: public offline fixture reference. Focused fixture and protocol checks
cover the local journey; release gates remain separate from this contract.

## Purpose

FitCheck's first product journey keeps a person's source, selected meaning,
and market-expression decision distinct. It is a prediction-market application
of the longer-term Epistemic Ledger direction: preserve a decision-ready thesis
with its evidence and a human-owned revision history. It is not a general
venture-discovery workflow, a trading surface, or a claim that a prediction
market proves a thesis true.

## Interaction contract

1. The person provides source text and may attach URL provenance.
2. The system proposes one to three distinct, source-grounded thesis
   candidates in source order. Each has an exact source excerpt and a concise
   advisory summary. The system does not infer which candidate matters most.
3. The person selects one candidate to develop or explicitly selects `none`.
   Unanswered, selected, and explicit `none` are separate states. Unselected
   candidates remain source evidence; they are not approved theses, human
   preferences, or review labels.
4. Only the selected candidate enters normalization. The system returns either
   a proposed thesis, one concrete clarification, or a rejection. Only an
   explicit `accept` approves the proposal and materializes a
   `ThesisAnalysis`. `edit` starts a successor proposal cycle and does not
   approve the predecessor; `reject` stops that attempt without materializing
   a thesis.
5. Only a human-approved thesis reaches the market pool. Retrieval rank,
   per-market assessment, and display rank remain distinct. The UI shows up to
   three assessed candidates with their resolution conditions, what each
   captures, what it misses, and frozen-snapshot provenance. The person selects
   a market or explicitly selects `none`.
6. `no_clean_expression` is an aggregate outcome only for the exact,
   non-empty, fully assessed displayed pool. It never claims that the complete
   market universe has been searched.

## State and authority rules

- The synchronous path binds
  `source_interpretation_id` -> `source_thesis_candidate_id` ->
  `source_candidate_choice_id` -> `normalization_attempt_id` ->
  `normalization_decision_id` -> `thesis_analysis_id` ->
  `candidate_set_id` plus its per-market `market_assessment_id` values ->
  `market_display_set_id` -> `market_choice_id`. Input and source-quote
  digests bind their corresponding records. A source change clears candidate
  selection, normalization, and market state; stale or cross-bound identities
  are errors.
- Models and providers propose. Deterministic code may enforce declared
  schemas, safety conditions, provenance, and bookkeeping; it does not
  establish semantic truth.
- Session users approve meaning and choose a market or `none`. Named reviewers
  own semantic adjudications. Product-session feedback may create review
  evidence, but it cannot directly rewrite canonical cards, decisions, or
  goldens.
- A future agent surface may perform bounded collection or preparation work
  only within a user's authorization. Monitoring, notifications, durable
  personal-thesis lineage, and automated revision are not established by this
  interaction contract.

## Evidence boundary

An implementation must supply its own synthetic fixtures, offline tests, and
browser verification for the exact states above before this contract can be
described as publicly runnable. Evaluation of semantic usefulness is separate:
it requires a versioned policy, appropriate human review, and explicitly
bounded claims.

This contract describes the synchronous local UI path. A generic MCP surface
may expose the same authority and identity rules through a bounded source-job
protocol with a separately run fixture worker. That protocol is not evidence
of an asynchronous UI, a production service, or verified compatibility with a
named client.
