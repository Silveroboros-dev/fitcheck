# SFT Fit Classifier Contract v1

Status: v1, governs the first Gemini supervised-tuning data export and
measurement harness for the FitCheck fit classifier. This contract does not
authorize a tuning job or product model swap. It defines the data boundary and
the evidence required before either is considered.

## Purpose

The SFT track exists to test whether a tuned Gemini classifier can improve on
the current raw Gemini baseline for market-fit classification without increasing
false-strong or direct-false-positive risk.

The tuned model, if created later, is a proposer/classifier candidate only. It
does not own golden labels, review truth, deterministic gates, or product
promotion decisions.

## Source Data

Training candidates come only from human-reviewed FitCheck rows exported as:

```text
data/eval_sets/reviewed_fit_testing_v1/reviewed_fit_testing_samples_v1.jsonl
```

The exporter must verify the reviewed-fit manifest before building SFT files.
Rows without a matching manifest are allowed only for ad-hoc local experiments
and must be marked unpinned.

Allowed training source:

- `clean_primary` reviewed rows.

Excluded from training:

- `validated_rejection_sentinels_v1`;
- `legacy_regression` rows;
- heldout rows;
- validation rows;
- unreviewed model outputs;
- production user feedback that has not passed human review.

## Splits

The exporter creates deterministic splits from a stable seed:

- train: clean reviewed examples eligible for tuning;
- validation: clean reviewed examples used by the tuning job;
- heldout: clean reviewed examples used only for final measurement;
- sentinel: validated rejection rows, always held out;
- regression: legacy rows, always held out.

Sentinel rows protect the highest-risk failure mode: weak/no-clean truth
overcalled as direct or indirect. They are never training examples for the
first SFT program.

## Output Files

The export command is:

```bash
.venv/bin/python scripts/export_sft_fit_classifier.py \
  --out-dir data/sft/fit_classifier_v1
```

It writes:

```text
fit_sft_train_v1.jsonl
fit_sft_validation_v1.jsonl
fit_sft_heldout_v1.jsonl
fit_sft_sentinel_v1.jsonl
fit_sft_regression_v1.jsonl
fit_sft_eval_inputs_v1.jsonl
fit_sft_manifest_v1.json
```

Tuning JSONL uses the Gemini supervised-tuning conversation shape:

- `systemInstruction`;
- `contents[0]` user prompt;
- `contents[1]` model JSON target.

The tuning target is the reviewed fit judgment:

- `fit_class`;
- `recommended_market_id`;
- `confidence`;
- `rationale`;
- `what_it_captures`;
- `what_it_misses`.

The target must not include review metadata, sample ids, split names, sentinel
flags, or hidden truth fields outside the JSON answer.

## Readiness Rule

The current reviewed corpus is a plumbing dataset, not a learning dataset,
until the train split reaches at least 100 examples.

Readiness states:

- `ready_for_plumbing_spike_only`: train split has fewer than 100 examples.
- `candidate_for_small_sft`: train split has at least 100 examples.

The practical target for a meaningful classifier read is 250-500 reviewed rows.
The practical target for stable improvement is 1000+ reviewed rows.

## Inference

The tuned-model inference command is:

```bash
.venv/bin/python scripts/run_sft_inference.py \
  --eval-inputs-jsonl data/sft/fit_classifier_v1/fit_sft_eval_inputs_v1.jsonl \
  --model "$FITCHECK_SFT_MODEL" \
  --output-jsonl /tmp/fit_sft_predictions.jsonl
```

`scripts/run_sft_inference.py` must fail closed without `--model` or
`FITCHECK_SFT_MODEL`. It must not fall back to `GEMINI_MODEL`, because that
would let a base-model run masquerade as a tuned-model run.

## Measurement

The offline measurement command is:

```bash
.venv/bin/python scripts/run_sft_measurement.py \
  --predictions-jsonl /tmp/fit_sft_predictions.jsonl \
  --eval-inputs-jsonl data/sft/fit_classifier_v1/fit_sft_eval_inputs_v1.jsonl \
  --arm-id sft_candidate \
  --model "$FITCHECK_SFT_MODEL" \
  --output-json docs/experiments/YYYY-MM-DD-sft-fit-measurement.json \
  --output-md docs/experiments/YYYY-MM-DD-sft-fit-measurement.md
```

The measurement harness scores:

- class accuracy;
- market match;
- false strong;
- direct false positive;
- overcall;
- undercall;
- sentinel slice behavior.

It is offline and must not call live models.

## Promotion Gate

No tuned model reaches production unless all are true on heldout plus sentinel
measurement:

- sentinel false strong = 0;
- direct false positives do not increase versus raw Gemini baseline;
- false strong count does not increase versus raw Gemini baseline;
- heldout class accuracy improves materially;
- direct recall does not collapse;
- market match does not materially regress;
- results are reproducible from committed export and prediction artifacts.

This is a candidate-model gate, not an automatic promotion. Product wiring
still requires explicit human approval.

## Claim Discipline

SFT reports must not claim "zero false positives" unqualified. The allowed
claim form is scoped:

```text
zero false strongs on the named heldout/sentinel measurement run
```

Any production claim must name the dataset version, split, model id, and date.

## Acceptance Criteria

- AC-1: the exporter verifies the reviewed-fit manifest when present.
- AC-2: sentinel and legacy rows are excluded from training.
- AC-3: SFT targets contain only the model-answer JSON, not hidden review
  metadata.
- AC-4: the manifest states readiness and the 100-example minimum.
- AC-5: inference fails closed without an explicit tuned model id.
- AC-6: measurement is offline and reports false strong plus direct false
  positives.
- AC-7: no tuned model promotion is allowed without heldout and sentinel gates.
