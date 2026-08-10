"""Draft-contract golden-property eval (blueprint §13 step 5b).

Eval-first, ZERO new labeling: the six golden draft strings already in
evals/data/eval_set.json (eval_002/003/006/007/008/010) are the property
oracle. A model-generated draft that passes the gate produces output of
the SAME shape as these goldens, so asserting structural properties over
them proves the gate's checks are calibrated to real good output, not
invented.

The goldens are free-text strings, not structured ProposedDraft objects,
so this harness checks the STRING-level analog of each structured gate
check:
- vocabulary-clean   (gate D2): no restricted terms;
- subject named      (gate D4): a claim subject token appears (tok_v1);
- deadline referenced(gate D3): the claim window-end year appears verbatim
                                (tok_v1 strips year tokens, so this is a
                                raw-string check — the structured gate
                                checks exact date equality instead);
- is a question      (gate shape): non-trivial, ends with '?'.

Model-free by construction (invariant #2): a CI test asserts this module
never imports a model adapter. It shares the gate's deterministic
primitives (vocabulary_violations, tok_v1) directly, so the oracle and
the gate test the same things.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from el.domain.structures import ExtractedStructure
from el.domain.vocabulary import vocabulary_violations
from el.fitgate.checks import TOKEN_RULES_VERSION, tok_v1

# The Phase 0 cases that carry a golden no_clean_draft_contract string.
EXPECTED_DRAFT_CASES: tuple[str, ...] = (
    "eval_002",
    "eval_003",
    "eval_006",
    "eval_007",
    "eval_008",
    "eval_010",
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DraftGoldenCase(_Model):
    case_id: str
    draft_text: str
    subjects: list[str]
    objects: list[str]
    metric_what: str
    window_end_year: int


class DraftGoldenResult(_Model):
    case_id: str
    vocabulary_clean: bool
    subject_named: bool
    object_metric_echoed: bool
    deadline_referenced: bool
    is_question: bool
    passed: bool


class DraftGoldenReport(_Model):
    token_rules_version: str = TOKEN_RULES_VERSION
    cases: list[DraftGoldenResult]
    expected_case_ids: list[str]
    missing: list[str]
    passed: bool


def _repo_root() -> Path:
    return Path(__file__).parents[2]


def _subjects(structure: ExtractedStructure) -> list[str]:
    subjects = [e.name for e in structure.entities if e.role == "subject"]
    return subjects or [e.name for e in structure.entities]


def load_draft_goldens(
    eval_set_path: str | Path, golden_claims_path: str | Path
) -> list[DraftGoldenCase]:
    labels = {c["id"]: c for c in json.loads(Path(eval_set_path).read_text())}
    claims = json.loads(Path(golden_claims_path).read_text())
    cases: list[DraftGoldenCase] = []
    for entry in claims["cases"]:
        case_id = entry["case_id"]
        label = labels.get(case_id)
        if label is None:
            continue
        draft = label["expected_fit_card"].get("no_clean_draft_contract")
        if not draft:
            continue
        structure = ExtractedStructure.model_validate(entry["structure"])
        cases.append(
            DraftGoldenCase(
                case_id=case_id,
                draft_text=draft,
                subjects=_subjects(structure),
                objects=[
                    e.name for e in structure.entities if e.role == "object"
                ],
                metric_what=structure.metric.what,
                window_end_year=structure.horizon.window_end.year,
            )
        )
    return cases


def _evaluate(case: DraftGoldenCase) -> DraftGoldenResult:
    vocabulary_clean = not vocabulary_violations(case.draft_text)
    text_tokens = tok_v1(case.draft_text)
    subject_named = any(tok_v1(s) & text_tokens for s in case.subjects)
    # D6 string analog: the claim's object (if any) and metric tokens
    # appear in the draft.
    object_tokens: set[str] = set()
    for name in case.objects:
        object_tokens |= tok_v1(name)
    object_ok = not case.objects or bool(object_tokens & text_tokens)
    metric_ok = bool(tok_v1(case.metric_what) & text_tokens)
    object_metric_echoed = object_ok and metric_ok
    deadline_referenced = str(case.window_end_year) in case.draft_text
    stripped = case.draft_text.strip()
    is_question = stripped.endswith("?") and len(stripped) > 20
    return DraftGoldenResult(
        case_id=case.case_id,
        vocabulary_clean=vocabulary_clean,
        subject_named=subject_named,
        object_metric_echoed=object_metric_echoed,
        deadline_referenced=deadline_referenced,
        is_question=is_question,
        passed=(
            vocabulary_clean
            and subject_named
            and object_metric_echoed
            and deadline_referenced
            and is_question
        ),
    )


def run_draft_golden_eval(
    eval_set_path: str | Path, golden_claims_path: str | Path
) -> DraftGoldenReport:
    cases = load_draft_goldens(eval_set_path, golden_claims_path)
    results = [_evaluate(case) for case in cases]
    found = {case.case_id for case in cases}
    missing = [cid for cid in EXPECTED_DRAFT_CASES if cid not in found]
    return DraftGoldenReport(
        cases=results,
        expected_case_ids=list(EXPECTED_DRAFT_CASES),
        missing=missing,
        passed=not missing and all(r.passed for r in results),
    )


def _default_paths() -> tuple[Path, Path]:
    root = _repo_root()
    return (
        root / "evals" / "data" / "eval_set.json",
        root / "tests" / "fixtures" / "retrieval" / "recall_golden_phase0.json",
    )


if __name__ == "__main__":
    report = run_draft_golden_eval(*_default_paths())
    print(f"draft golden-property eval — {report.token_rules_version}")
    print(
        f"  {'PASS' if report.passed else 'FAIL'} "
        f"({len(report.cases)} goldens; missing: {report.missing or 0})"
    )
    for result in report.cases:
        missed = [
            name
            for name in (
                "vocabulary_clean",
                "subject_named",
                "object_metric_echoed",
                "deadline_referenced",
                "is_question",
            )
            if not getattr(result, name)
        ]
        status = "ok" if result.passed else "MISS " + ",".join(missed)
        print(f"    {result.case_id}: {status}")
