"""Loop 4 — review-candidate intake and eval-delta-gated promotion.

The "improvement" half of the learning loop (Learning Loop v0):
- candidates.py   — gate signals -> ReviewCandidateSpec (model/DB-free).
- store.py        — persist specs to the review_candidates table.
- promotion.py    — before/after eval-delta + shipping gates (port of MFTA's
                    repair-loop verifier); GO promotes, else candidate-only.
- perturbation.py — label-free robustness signal (anti-bad-twin + anti-brittle).

Import from the submodules directly; this package re-exports nothing, to
keep the model/DB-free modules decoupled from the DB boundary.
"""
