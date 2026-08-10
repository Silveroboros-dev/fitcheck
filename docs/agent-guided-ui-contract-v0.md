# Agent-Guided UI Contract v0

Status: v0, governs the first Phase-1 product UI vertical slice
(`el/product/`). Written before implementation (mission requirement).
Scope: paste thesis → extract/normalize → retrieve candidates → classify
fit → Market Fit Card → conviction/blind prior → save → read back.
The experience may FEEL agent-guided; internally and visibly it is
loops-with-gates. The model proposes; deterministic gates and the human
own final authority. The existing Review Console (`el/review/`) is a
separate surface and is not touched by this contract.

## UI Modes / Surfaces

- **Mode `fixture` (default for local dev/smoke):** deterministic fixture
  proposers + the frozen Phase-0 market snapshot. No credentials, no live
  calls. Only the checked-in fixture claim texts extract successfully;
  any other input surfaces the extraction gate's explicit refusal or an
  unavailable state — never a fabricated result.
- **Mode `gemini`:** live Gemini proposers + env-selected market provider
  (mirrors `el.mcp.wiring`). Requires credentials; never required for UI
  smoke.
- **Surfaces (one page, four panels, strictly ordered):**
  S1 Intake workspace → S2 Blind prior → S3 Market Fit Card →
  S4 Conviction & save, plus S5 Ledger read-back (list + detail).
  A later panel never renders content for a different thesis than the
  panel above it (see Identity keys and Stale-state rules).

## Identity Keys (every stateful object)

| object | key | minted by |
| --- | --- | --- |
| thesis analysis | `thesis_analysis_id` (UUID) | ExtractionService on intake |
| blind prior / conviction event | `conviction_event_id` (UUID), odds lock scoped to (client_type=`human_ui`, actor_id=`user:<user_id>`) | LedgerService |
| candidate set | `candidate_set_id` (UUID) + `snapshot_id` / `as_of_ts` | RetrievalService |
| fit card | `fit_card_id` (UUID) | FitService |
| draft contract | `draft_contract_id` (UUID), back-linked from fit card | DraftContractService |
| ledger entry | `ledger_entry_id` (UUID), unique per (user, thesis) | LedgerService |
| local user | `user_id` (UUID) — single local user, get-or-created at startup; `agent_client_id = human_ui:<user_id>` scopes transient objects | product wiring |

Every UI action carries the explicit id of the object it acts on. No
action ever infers a target from panel order, most-recent row, or any
other implicit state.

## Data Source per Field

| field | source | kind |
| --- | --- | --- |
| input_text | user paste, echoed verbatim | raw source (A7-exempt) |
| normalized thesis summary, extracted structure | ExtractionService (proposer + deterministic gate `loop1-v1`) | system-generated, A7-checked |
| candidate markets, snapshot metadata (`snapshot_id`, `as_of_ts`, counts) | RetrievalService over the frozen snapshot (or live provider in `gemini` mode) | candidate evidence |
| market title / resolution rules shown on the card | persisted market snapshot rows for the recommended/rejected market ids | quoted market source (A7-exempt) |
| fit class, captures, misses, horizon match, resolution risk, confidence, rejected markets | FitService deterministic gate (`loop3-v1` family) | canonical verdict of the gate |
| current odds + side | LedgerService reveal (only after blind prior; thesis-side oriented) | market data at reveal time |
| draft contract preview | DraftContractService (proposer + `draftgen-v1` gate) | shape-valid draft candidate, A7-checked |
| conviction level, exposure bucket, justification, blind prior | user input | user-owned |
| odds_at_entry (+side), attestation, timestamps | LedgerService at save, copied from the frozen candidate member | ledger record |

## Canonical Truth vs Advisory/Candidate Data

- **Canonical for this UI:** the deterministic fit gate's verdict (fit
  class, captures/misses, risk fields), ledger rows, odds lock state.
- **Candidate/advisory, always labeled as such in the UI:** retrieved
  candidate markets (labeled with snapshot id + retrieved-at time and the
  words "candidate markets — retrieval evidence, not a verdict"), draft
  contracts (labeled "shape-valid draft candidate"), model-extracted
  structure (shown under the gate's acceptance).
- Candidate markets are NEVER presented as canonical truth (AC-3): the
  card shows the gate's classification OF a market; the candidate list is
  provenance metadata, not a recommendation list.
- Review-side truth (golden datasets, review decisions) is entirely
  outside this surface.

## Actions That Mutate Ledger State

Exactly three, all explicit button presses, all POST:

1. `submit blind prior` → creates conviction event + actor-scoped odds
   lock (write-once; server rejects a second submit after reveal/save).
2. `save to ledger` → creates the ledger entry (strict save: rejected
   with named violations until conviction level, exposure bucket, and
   justification are present; idempotent per user+thesis).
3. (implicit within classify) `reveal odds` → stamps `odds_revealed_at`
   write-once on the blind event. Classify is otherwise read/derive-only.

Nothing else mutates ledger state. Intake and classify persist their own
loop artifacts (thesis analysis, candidate sets, fit cards) — those are
loop provenance, not ledger state.

## Forbidden Actions (this surface)

- No wallets, custody, execution, order placement, or fake fills.
- No advice controls; no restricted vocabulary anywhere in system copy:
  "buy", "sell", "edge", "risk-free", "you should trade". Allowed
  vocabulary: best available expression, direct, indirect, weak proxy,
  no clean expression.
- No writes to review decisions, golden datasets, eval exports, or
  promotion artifacts.
- No mutation of fit verdicts from the UI (corrections flow exists only
  as review-candidate intake and is OUT of this v0 slice).
- No odds display before a blind prior exists for the thesis (server
  enforces; UI mirrors).
- No cross-user reads/writes; unknown or unowned ids return the same
  not-found shape (no existence leak).

## Stale-State Clearing Rules

- Editing the source text marks downstream panels stale immediately
  (visual + disabled actions); a successful new intake CLEARS blind
  prior, fit card, draft, conviction form, and save state in the UI.
- Every downstream panel renders only data fetched with the CURRENT
  `thesis_analysis_id` / `fit_card_id`; responses carry these ids and the
  client drops any response whose ids no longer match the current chain.
- Server-side, identity keys make cross-thesis binding impossible:
  classify requires a `thesis_analysis_id`, save requires a
  `fit_card_id`, and ownership checks reject foreign ids.
- The ledger read-back panel is history, not part of the active chain;
  it never renders as if it belonged to the current thesis unless the
  entry's `thesis_analysis_id` matches.

## Failure / Empty States (explicit, never silent)

- Extraction refusal → the gate's reasons, verbatim, with "no analysis
  was created".
- Proposer/model unavailable (missing credentials, dead adapter) → HTTP
  503 surface state: "model service unavailable — [detail]"; Run stays
  enabled for retry.
- Retrieval empty / no candidates → the card renders the no-clean state
  with zero-candidate provenance, not an error.
- `blind_prior_required` on classify → S2 highlighted, classify blocked
  until submitted.
- Save violations → per-field messages from the server's violation list;
  save button stays disabled until fields are complete.
- Not-found / stale ids → "this object no longer matches the current
  analysis — re-run".
- Empty ledger → "no saved entries yet".

## Acceptance Criteria

- **AC-1:** This contract exists and a test asserts its required
  sections and criteria ids are present.
- **AC-2:** Happy path works end-to-end in fixture mode: paste fixture
  thesis → extract → blind prior → classify → Market Fit Card → convict
  → save → read back the saved entry.
- **AC-3:** Candidate markets are labeled candidate evidence and never
  rendered as the verdict; the API response separates gate verdict from
  candidate metadata.
- **AC-4:** Changing the source thesis clears stale market/fit/ledger
  panel state; classify responses bind to the requesting thesis id.
- **AC-5:** No-clean expression is first-class: dedicated card state,
  draft-contract preview offered, save still possible (no linked market,
  no odds).
- **AC-6:** Ledger save is blocked (server-side violations + disabled UI)
  until conviction level, exposure bucket, and justification exist.
- **AC-7:** Missing model/retrieval services produce explicit unavailable
  states (503 + labeled panel), never fabricated output.
- **AC-8:** All system copy (API responses + static page) passes the
  restricted-vocabulary check.
- **AC-9:** Tests cover: contract sections, state binding by id,
  stale-state prevention, the no-clean path, and that the UI surface
  cannot mutate fit verdicts or review/golden data (forbidden-mutation
  checks).
- **AC-10:** A manual browser smoke is documented (local URL + click
  path) and was executed at least once before merge.

## Manual Smoke (AC-10)

```bash
.venv/bin/uvicorn el.product.app:app --port 8100
# open http://127.0.0.1:8100/
# 1. Paste: "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot
#    Arena by the end of 2026."  → Run analysis
# 2. Submit blind prior (e.g. 0.35, confidence medium)
# 3. Classify → Market Fit Card renders class + captures/misses + odds
# 4. Pick conviction/exposure, write justification → Save to ledger
# 5. Ledger panel → open the saved entry → verify odds-at-entry + ids
# No-clean path: repeat with the Zzcorp underwater-basket-weaving fixture
# text → no-clean card + draft preview; save without market/odds.
```
