# Reviewed-data measurement summaries

This page reports aggregate measurements from the private governed review
corpus. It is evidence about observed system behavior, including negative
results; it is not a release of the underlying rows or a claim of production
accuracy.

## July 2026 measurement read

The July experiments exposed limitations rather than supporting a
production-quality claim.

- The product-faithful gate pipeline attempted 64 reviewed rows and produced
  scorable output for 63. In selected-market scope it reached 34.9%
  classification accuracy with 6 false-strong classifications. A separate
  July plain-Gemini baseline scored all 64 reviewed rows at 51.6% accuracy
  with 2 false-strongs. Because the gate had one unrunnable row, this is a
  directional comparison rather than an exactly matched 63-row A/B.
- A zero-model-call shadow structure verifier reduced the gate result's
  selected-scope false-strong count from 6 to 2 when refused structures were
  treated as `no_clean_expression`. It was not production-ready: this in-sample
  diagnostic also falsely blocked three rows whose reviewed label was a direct
  expression.
- Same-model Best-of-N was a null result. Across 64 rows and eight draws per
  row, 57 rows were class-unanimous, including both persistent false-strong
  cases. Majority voting and deterministic verifier reranking each reached
  50.0%, below the 51.6% temperature-zero reference.

These results argue against adding more gate rules or same-model resampling as
default improvement paths without a new pre-registered reason. They remain
dataset-specific measurements and do not establish real-world accuracy,
calibration, or market coverage.

The underlying review corpus is private. The immediate next measurement is a
pre-registered held-out evaluation on fresh reviewed rows in August 2026,
using the same instruments.
