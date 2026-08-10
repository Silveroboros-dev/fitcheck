# Reviewed Reference Dataset Spec v0

Date: 2026-06-14
Status: implemented as local review dataset v0

## Purpose

Build a trustworthy reviewed reference dataset for FitCheck experiments:

- compare strict raw Gemini against FitCheck adapters and future policy variants;
- create preference-pair data for reward-ranker or direct-alignment-style experiments;
- preserve the exact thesis and market evidence a reviewer saw;
- keep model outputs advisory and human review authoritative for experiment labels.

Existing MFTA and FitCheck rows are source material. A case becomes
experiment-grade only after it has a valid review decision and passes
deterministic export checks.

## Core Artifacts

| Artifact | Mutability | Purpose |
| --- | --- | --- |
| `data/review/review_packets_v0.jsonl` | immutable per version | One reviewable thesis-market packet per line. |
| `data/review/review_decisions_v0.jsonl` | append-only by default; latest-row correction allowed only through explicit correction mode | Human review decisions. |
| `data/review/review_taxonomy_v0.json` | versioned | Approved failure taxonomy and aliases. |
| `data/review/market_snapshots_v0.jsonl` | immutable per version | Frozen market evidence used by packets. |
| `data/review/candidate_judgments_v0.jsonl` | immutable per run | Raw Gemini / FitCheck A/B / future advisory outputs. |
| `data/review/exports/reference_labels_v0.jsonl` | derived | Reviewed real-world cases eligible for primary experiments. |
| `data/review/exports/stress_suite_v0.jsonl` | derived | Reviewed or accepted stress cases, separate from real-world metrics. |
| `data/review/exports/preference_pairs_v0.jsonl` | derived | Human-approved pairwise preferences among candidate judgments. |

## Identity Keys

Every transfer step must preserve explicit identity:

- `packet_id`
- `source_dataset`
- `source_case_id`
- `source_row_hash`
- `market_snapshot_ids`
- `market_snapshot_selection`
- `selected_market_snapshot_id`
- `candidate_judgment_ids`
- `review_decision_id`
- `reviewer_id`
- `dataset_version`

Do not infer active context from row order, selected UI state, or the first
available candidate. Every UI/API action must carry `packet_id`.

## Market Snapshot Requirement

For experiment-grade reviewed cases, freeze the exact market evidence the
reviewer sees. The required object is a rules snapshot, not a full market
universe or order book.

Required market fields:

- `market_snapshot_id`
- `market_id`
- `venue`
- `url`
- `title`
- `description`
- `resolution_rules`
- `outcomes`
- `close_date`
- `status`
- `captured_at_utc`
- `snapshot_kind`
- `retrieval_provider`
- `retrieval_id`
- `retrieval_query`
- `polydata_cutoff_at_utc`
- `market_role`

Snapshot policy:

- Governance-50: fetch/freeze best, acceptable, rejected, and tempting markets
  when IDs are available.
- v2/v3 source candidates: fetch/freeze referenced candidate markets before
  review.
- Stress-40: no live snapshot required when market text/rules are embedded;
  add snapshots only if cases map cleanly to real market IDs.
- New intake: freeze candidate market evidence at intake time.
- Odds and price history are optional for fit classification.
- Human review must choose a market snapshot outcome before a case can enter
  reviewed reference or stress exports: either exactly one attached
  `selected_market_snapshot_id`, or explicit `market_snapshot_selection=none`
  when no proposed snapshot is relevant. Candidate packets may carry multiple
  proposed snapshots, but cleared exports keep only the reviewer-selected
  snapshot or the explicit no-market choice to avoid ambiguous class evidence.

## Live Snapshot Fill Contract

Live market retrieval is candidate evidence. It may make a packet reviewable,
but it does not create a reviewed label, select a market, or rewrite any prior
review decision.

The fill workflow is script-driven and dry-run by default:

- input identity is `packet_id`;
- packets without proposed/normalized thesis text are skipped visibly;
- unreviewed packets with proposed thesis but no snapshots may receive attached
  candidate `market_snapshot_ids` when `--apply` is explicit;
- reviewed packets that need more markets are cloned into second-review packets
  when `--include-reviewed-followups --apply` is explicit; the original packet
  and decision remain unchanged;
- every added snapshot carries `snapshot_kind`, `retrieval_provider`,
  `retrieval_id`, `retrieval_query`, `captured_at_utc`, and
  `polydata_cutoff_at_utc`;
- every run writes or previews a `snapshot_fill_manifest_v0` row per target;
- the script must never modify `review_decisions_v0.jsonl` or
  `candidate_judgments_v0.jsonl`.

Retrieved rows are proposed market evidence only. The reviewer must still
choose one attached snapshot or `none` before a case can enter a reviewed
reference or stress export.

## Normalization Repair Contract

Gemini or another proposer may suggest a better normalized thesis for a packet
that was rejected because the old thesis was weak, absent, or pointed at the
wrong source claim. These suggestions are advisory repair candidates, not label
edits.

The normalization-repair workflow is script-driven and dry-run by default:

- input identity is `source_case_id` plus the parent `packet_id`;
- an explicit `--apply` is required before writing any packet or manifest row;
- each accepted proposal becomes a new unreviewed packet with a new
  `packet_id` and `source_case_id`, preserving the parent packet ID, parent
  decision ID, model run ID, gate verdict, prior thesis, and clarifying
  question in `source_provenance`;
- the original packet and all historical review decisions remain unchanged;
- existing attached market snapshots may be reused as candidate evidence, but
  the reviewer must still choose one snapshot or `none`;
- `candidate_judgment_ids` remain empty unless a separate advisory fit-judgment
  run has produced reviewable judgments;
- the script must never modify `review_decisions_v0.jsonl`,
  `candidate_judgments_v0.jsonl`, or `market_snapshots_v0.jsonl`.

Normalization repair packets are eligible for clean-golden export only after
normal human review. The model proposal cannot promote itself, rewrite a prior
rejection, or become canonical truth without a reviewer decision.

## Reviewed Fit Testing Dataset Contract

The testing dataset is broader than clean goldens. It may include reviewed
legacy goldens for regression coverage, but those rows must be clearly tiered
away from the clean source of truth.

The reviewed fit testing export:

- includes all `clean_goldens_v1` rows as `evaluation_tier=clean_primary`;
- may include reviewed `fitcheck_legacy_goldens` rows as
  `evaluation_tier=legacy_regression`;
- excludes any packet with a historical reject signal;
- requires a high-confidence, usable, include-reference, keep/change latest
  review decision;
- preserves source, normalized thesis, review, selected market, and candidate
  market snapshots;
- stamps legacy rows with qualification warnings such as `source_unavailable`;
- must never back-promote legacy rows into `clean_goldens_v1`.

## Review Console Contract

The review console is a local tool, not a public product surface. The local
implementation is a FastAPI server plus static HTML. Normal review mode may
append review decisions; correction mode may replace one existing latest review
decision in place when the reviewer is explicitly editing their prior answer.
It must not mutate packets, snapshots, source data, or advisory candidate
judgments except through the explicit reviewer snapshot-edit endpoint.
That endpoint may prepend a new reviewer-authored frozen market snapshot to the
active packet when the reviewer is repairing fixture-derived market evidence;
existing snapshot references remain available for historical audit. It must not
mutate review decisions or advisory candidate judgments.

Required API:

- `GET /review/packets`
- `GET /review/packets/{packet_id}`
- `POST /review/packets/{packet_id}/market-snapshots`
- `POST /review/decisions`
- `GET /review/corrections`
- `PUT /review/decisions/{review_decision_id}`
- `GET /review/exports/reference`
- `GET /review/exports/stress`
- `GET /review/exports/preferences`
- `GET /review/exports/clean-goldens`

Required UI behavior:

- show source text, provenance, normalized claim, market snapshots, and current
  source label/status;
- show a prominent review-focus summary before the normalized claim when the
  packet identity or comparison market is the reviewer task, including
  `source_case_id`, current source fit class, best market ID, comparison market
  title, source notes, pending same-normalized-thesis count, and
  duplicate-payload count when present;
- order displayed market snapshots for review clarity without mutating packet
  references: reviewer-authored edits first, then the current source/best market
  or other fixture snapshots, then generic live candidate retrievals;
- show candidate judgments as advisory outputs;
- make review fields explicit: fit class, confidence, case quality, taxonomy,
  false-strong risk, chosen market snapshot, optional preferred judgment,
  recommendation, notes;
- accept `no_failure` as the canonical failure taxonomy for clean matches,
  with common aliases such as `no failure` normalized on submit;
- hide advisory preference controls when a packet has no advisory candidate
  judgments;
- collapse exact duplicate review payloads in the default queue, while allowing
  duplicate-review mode to show non-rejected duplicates explicitly;
- keep rejected packets and exact duplicate payloads of rejected packets out of
  review queues, including live-fill second-review clones whose parent packet
  was rejected; full audit can still show them explicitly;
- treat rejection as terminal for review queues, exports, and live-fill
  follow-ups when any historical decision for the packet has `reject_case`,
  `recommendation=reject`, `case_quality=reject`, or a clear reject note;
- mark missing submission-blocking fields with a red outline until they are
  filled;
- allow switching packets with incomplete unsaved review fields; switching
  clears unsaved form state, and submit remains the only required-field gate;
- allow reviewer-authored market snapshot edits from the active packet's market
  snapshot panel; edited snapshots are frozen review evidence, not PolyData
  retrieval truth;
- submit only append-only review decisions;
- expose exactly two reviewer queues: pending review and decision-quality
  corrections; the one-off weak-proxy recheck queue is retired and must not
  appear in the UI or API;
- allow explicit correction of a latest review decision from the correction
  queue by replacing the existing `review_decision_id` row in place, preserving
  `review_decision_id` and `reviewed_at_utc`, and without appending a new
  review record;
- mark every successful correction-mode submit as `recheck_status=cleared` on
  that same decision row, even when the reviewer keeps the original fit class;
  correction queues exclude cleared decisions;
- reject correction attempts against non-latest decision rows, mismatched
  `packet_id`, unknown snapshots, or missing required fields;
- require direct, indirect, and weak-proxy decisions to choose exactly one
  attached market snapshot; reserve `market_snapshot_selection=none` for
  `no_clean_expression`;
- expose a decision-quality correction queue for objective consistency
  blockers and explicit manual correction requests, such as
  `no_clean_expression` rows with a selected market snapshot,
  direct/indirect/weak-proxy rows without one, or rows marked
  `manual_correction_requested_v1`; this queue uses the same existing-decision
  correction path and excludes terminally rejected packets;
- label correction mode so the reviewer knows the edit updates an existing
  decision row rather than creating new review history;
- label model outputs so they cannot be mistaken for reference labels.
- expose clean golden exports as a separate curated source-of-truth artifact,
  never as a rewrite of raw review packets or decisions.

Clean golden v1 qualification:

- source scope is non-synthetic;
- source URL exists and is not `source-unavailable`;
- normalized thesis text exists;
- latest review is high-confidence, usable, include-reference, and keep/change;
- no historical decision for the packet contains a reject signal;
- every promoted sample has at least one live/frozen candidate market snapshot
  with market ID, URL, and resolution rules;
- direct, indirect, and weak-proxy rows require one reviewer-selected market
  snapshot;
- no-clean-expression rows require explicit `market_snapshot_selection=none`
  plus candidate market snapshot evidence showing no acceptable expression.

## Acceptance Criteria

- AC-1: A written contract exists and declares all identity keys.
- AC-2: Review packet generation preserves source identity, provenance, source
  scope, and market snapshot references.
- AC-3: Market snapshots freeze the exact rules evidence visible to reviewers.
- AC-4: Review decisions are append-only and schema-valid.
- AC-5: Candidate judgments are stored and displayed as advisory only.
- AC-6: The UI binds every action to an explicit `packet_id`.
- AC-7: Switching packets clears stale unsaved review state.
- AC-8: Taxonomy terms are controlled by `review_taxonomy_v0.json`.
- AC-9: Primary reference export includes only reviewed, usable,
  non-disputed real-world cases.
- AC-10: Stress cases export separately from real-world reference cases.
- AC-11: Preference pairs require a valid human review decision.
- AC-12: Source-to-packet-to-UI-to-decision transfer is covered by tests.
- AC-13: Model outputs cannot write or modify reference labels.
- AC-14: Missing snapshots or invalid source pointers fail visibly, not silently.
- AC-15: Reviewed reference and stress exports include exactly one
  reviewer-selected market snapshot, or explicit `market_snapshot_selection=none`,
  per exported row.
- AC-16: Missing submission-blocking fields are visibly marked in red until
  completed.
- AC-17: Advisory preference controls are visible only when advisory candidate
  judgments exist for the active packet.
- AC-18: Clean/direct matches can use canonical taxonomy term `no_failure`;
  common spellings are normalized before saving.
- AC-19: Live snapshot fill is dry-run by default and requires explicit
  `--apply` before writing packets, snapshots, or a manifest.
- AC-20: Live snapshot fill skips packets that lack proposed thesis text.
- AC-21: Live snapshot fill never mutates review decisions or candidate
  judgments.
- AC-22: Reviewed follow-up packets are cloned for second review instead of
  rewriting the original reviewed packet.
- AC-23: The default review queue collapses exact duplicate payloads so repeated
  source/thesis/market evidence does not appear as fresh review work;
  duplicate-review mode can include non-rejected duplicates.
- AC-24: Rejected samples do not return to review queues, including exact
  duplicate payloads and second-review live-fill clones of rejected parent
  packets; full audit can still show them.
- AC-25: A reject signal in any historical decision for a packet excludes that
  packet from reference/stress exports and live-fill follow-up cloning, even if
  a later decision row is otherwise export-eligible.
- AC-26: Clean golden v1 exports are materialized separately from raw reviews
  and contain full source, normalized thesis, reviewer, selected-market, and
  candidate-market snapshot attributes.
- AC-27: Clean golden v1 excludes synthetic stress rows, missing-source rows,
  rejected rows, rows without normalized thesis, and rows without qualifying
  live/frozen market snapshot evidence.
- AC-28: Reviewers can attach reviewer-authored market snapshot repairs from the
  active packet, while review decisions and advisory judgments remain
  append-only/read-only.
- AC-29: The review console makes duplicate-thesis/adversarial packets
  decision-complete by surfacing the comparison market and source-case rationale
  before repeated normalized-thesis text, while preserving packet identity and
  immutable snapshot references.
- AC-30: The retired weak-proxy recheck flow is not exposed in the review UI or
  API; review work enters only through pending review or decision-quality
  corrections.
- AC-31: Correction mode can replace only the latest decision for a packet,
  preserves the existing `review_decision_id` and `reviewed_at_utc`, does not
  append a new decision row, marks the existing row `recheck_status=cleared`,
  and cannot mutate packets, snapshots, or advisory candidate judgments.
- AC-32: Direct, indirect, and weak-proxy review decisions require exactly one
  attached selected market snapshot; only no-clean-expression decisions may use
  `market_snapshot_selection=none`.
- AC-33: Decision-quality correction mode lists objective consistency blockers
  and explicit manual correction requests, excludes terminally rejected packets,
  prefills the existing latest decision, and clears cases only by updating the
  existing decision row so the blocker no longer exists.
- AC-34: Normalization repair proposals are dry-run by default, create new
  unreviewed packets only on explicit apply, preserve parent/proposal
  provenance, reuse attached snapshots as candidate evidence, and never mutate
  review decisions, candidate judgments, or market snapshots.
- AC-35: Reviewed fit testing exports include clean-primary and
  legacy-regression tiers explicitly, preserve review and market evidence, mark
  legacy source-quality warnings, exclude rejected packets, and never change the
  clean-golden export boundary.

## Required Test Suite

### `tests/test_review_dataset_contract.py`

- `test_contract_contains_acceptance_criteria`
- `test_contract_declares_identity_keys`
- `test_contract_declares_mutation_boundary`
- `test_contract_declares_advisory_model_outputs`

### `tests/test_review_packet_builder.py`

- `test_governance50_packet_preserves_source_fields`
- `test_stress40_packet_is_marked_synthetic_stress`
- `test_v2_v3_candidates_are_source_only`
- `test_packet_hash_changes_when_source_changes`
- `test_builder_does_not_write_review_decisions`

### `tests/test_review_jsonl_schema.py`

- `test_packets_jsonl_schema_valid`
- `test_decisions_jsonl_schema_valid`
- `test_market_snapshots_jsonl_schema_valid`
- `test_candidate_judgments_jsonl_schema_valid`
- `test_every_decision_references_existing_packet`
- `test_every_packet_snapshot_ref_exists`
- `test_every_candidate_judgment_references_existing_packet`
- `test_packet_ids_are_unique`
- `test_decision_ids_are_unique`
- `test_latest_decision_resolution_is_deterministic`
- `test_taxonomy_terms_are_approved`
- `test_invalid_fit_class_rejected`

### `tests/test_review_exports.py`

- `test_reference_export_excludes_unreviewed_disputed_rejected`
- `test_low_confidence_cases_excluded_from_primary_metrics`
- `test_stress_cases_exported_separately`
- `test_preference_pairs_require_human_review`
- `test_false_strong_cases_are_tagged`
- `test_source_traceability_in_every_export_row`

### `tests/test_review_console_api.py`

- `test_list_packets_returns_sorted_queue`
- `test_get_packet_returns_full_context`
- `test_submit_review_appends_decision`
- `test_submit_review_does_not_mutate_packets`
- `test_submit_review_rejects_invalid_taxonomy`
- `test_submit_review_rejects_unknown_packet`
- `test_model_outputs_are_read_only`
- `test_missing_snapshot_fails_visibly`
- `test_submit_review_requires_selected_market_snapshot`
- `test_submit_review_accepts_explicit_no_market_selection`
- `test_submit_review_rejects_snapshot_not_attached_to_packet`
- `test_submit_review_accepts_without_preferred_advisory_when_none_available`
- `test_submit_review_normalizes_no_failure_taxonomy_alias`
- `test_reviewed_packet_removed_from_queue_by_packet_id`

### `tests/test_review_console_ui.py`

- `test_review_page_contains_required_controls`
- `test_review_page_binds_packet_id_explicitly`
- `test_review_page_labels_candidate_judgments_as_advisory`
- `test_review_page_requires_chosen_market_snapshot`
- `test_review_page_marks_missing_blockers_red`
- `test_review_page_hides_advisory_preference_controls_when_unavailable`

Preferred browser coverage, when a browser test dependency is acceptable:

- `test_switching_packets_clears_stale_form_state`
- `test_review_submit_updates_only_current_packet`
- `test_reviewed_packet_status_changes_after_submit`
- `test_invalid_submit_shows_error_without_append`

## Experiment Trust Gate

The dataset can be used as a trusted experiment source only when all are true:

- contract tests pass;
- JSONL schema and referential-integrity tests pass;
- UI/API append-only tests pass;
- primary reference export contains only reviewed, usable, non-disputed
  real-world cases;
- stress export is separate;
- preference-pair export contains only reviewed packets;
- every exported row links back to source packet, review decision, and either
  one selected market snapshot or an explicit no-market choice;
- every exported reference/stress row includes one `selected_market_snapshot_id`,
  or `market_snapshot_selection=none`, not the packet's full candidate snapshot
  list;
- no model output is able to mutate reviewed labels.
