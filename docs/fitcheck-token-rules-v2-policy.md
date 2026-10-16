# FitCheck token-rule successor

Status: explicit implementation candidate; ordinary discovery stays on its
existing `loop3-m1-subject-only-v4` / `tok-v1` policy.

FC-R02 corrects the order of stopword removal. Historical `tok_v1("this")`
returns `{"thi"}` because stemming occurs before stopword lookup. The explicit
`tok-v2-stopwords-before-stem` successor removes raw stopwords before applying
all unchanged v1 rules. It also retains the predecessor's post-stem stopword
lookup. Bare years from 1900 through 2099, naive plural stemming, aliases,
direction guards, stacking, and quote thresholds keep their existing rules.

Select `TokenV2FitPolicy()` or `stopword_corrected_fit_policy()` explicitly.
Its fit identity is `loop3-m1-subject-only-tok-v2`. The policy dispatcher uses
those tokens for M1, the subject-only residual guard, E1, and composite sidecar
leg matching. P1's recognized direction vocabulary contains no affected raw
stopword and its annotation remains unchanged. The frozen `checks.py` and
`policy.py` remain byte-identical for historical replay.

Advisory quote checks must receive the selected `token_rules_version`; their
default remains `tok-v1`. Under the successor, verbatim `this always` and
`this revenue` have only one content token and fail the unchanged two-token
minimum. `this quarterly revenue` and `quarterly revenue decline` pass.
This does not adopt any separate policy permitting restricted vocabulary in
source quotes.

Job manifests pin both identities. Restoration rejects a token identity that
differs from its named fit policy. Mixed fit identities, mixed token identities,
and forged single-verdict token bindings fail before aggregation. Per-market
verdicts and worker provenance carry the selected token version. Service
callers must propagate the same version into quote checks and provenance.

`tests/test_token_rules_v2.py` records development token, quote, and check
deltas, including years and plural edge cases, and exercises replay, composite
coverage, and fail-closed aggregation. An offline comparison of the existing
exposed fixtures covered 22 labeled cases and 418 cross-pool pairs with zero
case-verdict or semantic check-vector changes. The synthetic probes expose
the intended M1/E1 changes where `this` previously supplied the only overlap.
No frozen labels or thresholds were edited; these checks do not establish a
semantic-accuracy improvement or authorize golden promotion.

Run the focused offline checks with:

```bash
python -m pytest tests/test_token_rules_v2.py tests/test_fitgate_checks.py \
  tests/test_advisory_source_quotes_policy.py tests/test_fit_policy_runtime.py \
  tests/test_token_policy_service.py \
  tests/test_classification_worker.py tests/test_fit_service.py
```
