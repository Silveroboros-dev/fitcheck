# Correction-Loop UI Contract v0

Status: v0, governs the correction/rejection surface added to the product
UI (`el/product/`). Written before implementation. Extends
`docs/agent-guided-ui-contract-v0.md`; everything there (identity keys,
stale-state rules, blind-prior ordering, restricted vocabulary, forbidden
actions) remains binding. The Review Console stays a separate surface.

## The invariant (binding, machine-checked where possible)

**A product correction mints a PENDING `review_candidates` row through the
existing correction/rejection intake path — and does nothing else.**

- Destination: the product database's `review_candidates` table (the
  Step-8 triage queue, `el/review/queue.py`), via the same intake
  semantics as the MCP `correct_fit` / `reject_market` tools.
- For the human_ui actor, sources are `USER_CORRECTION` /
  `USER_REJECTION` (the human-origin values already defined in
  `ReviewSource`).
- Never touched by this surface, directly or transitively: golden
  datasets, `data/review/*` (the governed JSONL reference root), reviewed
  decisions, fit cards / fit verdicts, market recommendations, ledger
  entries, odds locks. A correction is *about* a fit card; it never
  *changes* one.
- No auto-promotion: candidates are born `pending` and only the existing
  human triage (`queue.py`) and promotion (`promotion.py`) paths move
  them — this surface calls neither.

## Surfaces (extends the S3 Market Fit Card panel)

- **Correct fit class** — one control on the card: pick a different fit
  class (closed vocabulary: direct / indirect / weak proxy / no clean
  expression) + a required free-text note ("what did the gate miss?").
- **This market does not express my thesis** — one control per displayed
  market (the recommended market and each rejected-tempting-markets row):
  required free-text reason.
- Both controls appear only when a card is rendered and bind to the
  current `fit_card_id` (chain rules from the v0 contract apply: a stale
  card drops the controls with the card).

## Honesty rules (the UX core of this contract)

1. **The card never re-renders as if the correction took effect.** After
   a correction the verdict, class chip, odds, and captures/misses stay
   exactly as the gate published them. The UI adds a clearly separate
   acknowledgment: "recorded as review candidate `<id>` — pending human
   review; this card is unchanged."
2. A repeated identical correction (same card, same payload) returns the
   existing candidate — surfaced as "already recorded", not as a new
   submission.
3. No correction editing or deletion from this surface: intake is
   append-only; mistakes are corrected by submitting a better correction,
   which the reviewer sees side by side.
4. System copy stays inside the allowed vocabulary; the user's note and
   reason are raw user text (A7 source-exempt), everything else is
   checked.

## API (product surface additions)

- `POST /api/fit-card/{fit_card_id}/correct`
  `{corrected_class, note}` → `{review_candidate_id, status: "pending",
  already_recorded: bool}`
- `POST /api/fit-card/{fit_card_id}/reject-market`
  `{market_id, reason}` → same response shape.
- Validation: `corrected_class` ∈ the closed fit-class vocabulary;
  `note`/`reason` non-empty after trim; `market_id` must be a market this
  card actually surfaced (recommended or listed-rejected) — rejecting a
  market the card never showed is a 422, not intake.
- Ownership: same no-existence-leak rule as the v0 contract (unknown OR
  unowned `fit_card_id` → identical 404).
- Payload provenance recorded in the candidate row: actor id, client
  type `human_ui`, thesis/card ids, and the correction payload —
  deterministic note encoding, matching the MCP intake's shape.

## Forbidden actions (additions to the v0 list)

- No write path from these endpoints to fit cards, recommendations,
  ledger rows, decisions, goldens, or `data/review/*`.
- No gate re-run, re-classification, or "apply my correction" flow.
- No display of corrected class as the card's class anywhere in the UI.
- No promotion, triage, accept/reject of candidates from this surface.
- No correction submission before a card exists in the current chain.

## Failure / empty states

- Missing/blank note or reason → 422 with the field named; controls stay
  enabled.
- Unknown market for this card → 422 "market was not part of this card".
- Stale chain (thesis changed since render) → controls cleared with the
  card (v0 stale-state rules); a late response for a superseded card is
  dropped client-side.
- Duplicate → 200 with `already_recorded: true` and the original id.

## Acceptance criteria

- **CC-1:** This contract exists and a test asserts its required sections.
- **CC-2:** Correcting a fit class from the UI creates exactly one
  `review_candidates` row (`user_correction`, `pending`) and read-back
  shows the card unchanged.
- **CC-3:** Rejecting a surfaced market creates exactly one
  `user_rejection` pending row; rejecting an unsurfaced market is a 422.
- **CC-4:** Byte-level forbidden-mutation proof: after corrections, fit
  cards, market recommendations, ledger entries, and review decisions
  are identical to their pre-correction state (asserted in tests).
- **CC-5:** Idempotency: an identical resubmission returns the same
  candidate id with `already_recorded: true` and the row count is
  unchanged.
- **CC-6:** The card UI renders the pending acknowledgment without
  altering the verdict chip/odds/captures; no restricted vocabulary is
  introduced (page-level scan stays green).
- **CC-7:** Rendered browser evidence: the full correction and rejection
  flows exercised in the running app (fixture mode), with the candidate
  count observed via the API and screenshots captured — never declared
  done from code inspection alone.

## Manual smoke (extends the v0 click path)

```bash
.venv/bin/uvicorn el.product.app:app --port 8100
# LMSYS fixture -> run -> prior -> classify -> card renders
# 1. "Correct fit class" -> weak proxy + note -> pending acknowledgment;
#    verdict chip still shows the gate's class
# 2. Rejected-markets wall -> "does not express my thesis" on one row ->
#    reason -> pending acknowledgment
# 3. Repeat step 1 verbatim -> "already recorded", same candidate id
# 4. /api/health shows review_candidates count incremented by 2, not 3
```
