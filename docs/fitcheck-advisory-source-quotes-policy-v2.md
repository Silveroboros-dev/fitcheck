# FitCheck advisory source quotes policy v2

Date: 2026-10-14
Status: owner-selected policy; implementation remains opt-in
Finding: FC-Q01, owner policy decision
Predecessor: `fit-advisory-vocab-v1`
Successor: `fit-advisory-source-quotes-v2`

The owner permits restricted vocabulary only inside verified exact source
quotes. Generated advice remains prohibited. This decision does not enable
advisory, change the advisory cap table, or promote evaluation material.

The successor applies the exception only to `claim_evidence` and
`market_evidence`. Every claim quote must be a literal substring of retained
raw `input_text`, not extracted model structure. Every market quote must be a
literal substring of the bound frozen contract/rules capture. Case and
whitespace changes are not exact quotes under this successor. Each quote
still needs at least two content tokens under the explicitly selected token
rules. Identity and complete-condition checks remain mandatory.

All generated fields remain vocabulary-checked: captures, misses, falsifier,
bridge assumptions, card summaries, and rejected-market reasons. Source quotes
are evidence of the model's condition judgment, not an endorsement of the
quoted advice or certification of semantic accuracy.

An accepted successor records its vocabulary version, raw-input digest,
snapshot identity, contract/rules hashes, and token version. A full-card UI
response may exempt the declared failure evidence leaves only after verifying
those bindings against retained source data and rechecking the literal spans
and content-token floor. Other fields remain guarded. The predecessor MCP
card response retains its trimmed provenance and generated explanation guard;
it does not expose these evidence leaves.

These SHA-256 bindings detect inconsistent or accidental mutation inside the
application trust boundary; they do not authenticate the database against a
privileged writer who coherently rewrites both a quote and its digest. The
current threat model trusts durable storage administration. Extending it would
require an independently sealed candidate-graph receipt rather than another
unkeyed field digest.

Missing, invented, or thin evidence is rejected. FitService keeps its retry
budget of one, then returns the deterministic fallback with null confidence
and recorded rejection reasons. `fit-advisory-vocab-v1` remains the default:
it checks evidence vocabulary too, and missing historical version metadata
means that predecessor policy. Historical rows are not rewritten.

Selecting the successor requires the explicit `advisory_vocabulary_policy`
FitService argument (or `vocabulary_policy_version` and source corpora at the
merge gate). Golden re-review, live provider calls, and deployment remain
separately governed work.
