"""Loop 1 — gate verdicts, service ordering, fixture validity."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from el.domain.contracts import ThesisAnalysisIn
from el.domain.enums import ClientType
from el.domain.tables import Base, ThesisAnalysis
from el.extraction.gate import (
    GateVerdict,
    insider_screen,
    normalized_claim_gate,
)
from el.extraction.service import ExtractionService
from el.models.adapter import ExtractionProposal, FixtureProposer

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "claims"


def _load_fixtures() -> dict[str, ExtractionProposal]:
    out = {}
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        out[raw["input_text"]] = ExtractionProposal.model_validate(
            raw["proposal"]
        )
    return out


FIXTURES = _load_fixtures()
CLEAN = "Gemini is going to be ranked #1 chatbot on LMSYS Chatbot Arena by the end of 2026."
AMBIGUOUS = "OpenAI is going to lose enterprise traction by the end of 2026."


def test_all_golden_fixtures_validate():
    # Governed-seed discipline: every fixture must satisfy frozen schema v1.
    assert len(FIXTURES) >= 2
    for proposal in FIXTURES.values():
        assert proposal.structure.schema_version == 1


def test_clean_claim_passes():
    result = normalized_claim_gate(CLEAN, FIXTURES[CLEAN])
    assert result.verdict is GateVerdict.PASS
    assert result.structure is not None
    assert result.gate_policy_version == "loop1-v1"


def test_ambiguous_claim_needs_exactly_one_question():
    result = normalized_claim_gate(AMBIGUOUS, FIXTURES[AMBIGUOUS])
    assert result.verdict is GateVerdict.NEEDS_CLARIFICATION
    assert isinstance(result.clarifying_question, str)
    assert result.clarifying_question.count("?") == 1


def test_fallback_question_when_proposer_silent():
    silent = FIXTURES[AMBIGUOUS].model_copy(
        update={"clarifying_question": None}
    )
    result = normalized_claim_gate(AMBIGUOUS, silent)
    assert result.verdict is GateVerdict.NEEDS_CLARIFICATION
    assert result.clarifying_question  # deterministic template kicks in


def test_vocabulary_violation_rejected():
    bad_structure = FIXTURES[CLEAN].structure.model_copy(
        update={"claim_summary": "Gemini ranks #1 so you should trade it"}
    )
    bad = ExtractionProposal(structure=bad_structure)
    result = normalized_claim_gate(CLEAN, bad)
    assert result.verdict is GateVerdict.REJECTED_INVALID
    assert "restricted vocabulary" in result.reasons[0]


def test_insider_screen_examples():
    assert insider_screen("my employer's internal numbers show churn doubling")
    assert insider_screen("Based on insider information, OpenAI will miss Q3")
    assert insider_screen("I saw a leaked internal memo about layoffs")
    assert not insider_screen(CLEAN)
    assert not insider_screen(AMBIGUOUS)


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/loop1.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _service(session_factory, fixtures=None):
    return ExtractionService(
        FixtureProposer(fixtures if fixtures is not None else FIXTURES),
        session_factory,
    )


def test_pass_persists_analysis(session_factory):
    svc = _service(session_factory)
    outcome = svc.analyze(
        ThesisAnalysisIn(input_text=CLEAN, client_type=ClientType.HUMAN_UI)
    )
    assert outcome.result.verdict is GateVerdict.PASS
    assert outcome.analysis is not None
    assert outcome.model_adapter == "fixture"
    with session_factory() as s:
        rows = s.scalars(select(ThesisAnalysis)).all()
        assert len(rows) == 1
        assert rows[0].extracted_structure["schema_version"] == 1


def test_clarification_persists_nothing(session_factory):
    svc = _service(session_factory)
    outcome = svc.analyze(
        ThesisAnalysisIn(input_text=AMBIGUOUS, client_type=ClientType.AGENT_MCP)
    )
    assert outcome.result.verdict is GateVerdict.NEEDS_CLARIFICATION
    assert outcome.analysis is None
    with session_factory() as s:
        assert s.scalars(select(ThesisAnalysis)).all() == []


def test_insider_refused_before_model_call(session_factory):
    proposer = FixtureProposer({})  # any call would raise KeyError
    svc = ExtractionService(proposer, session_factory)
    outcome = svc.analyze(
        ThesisAnalysisIn(
            input_text="Our confidential numbers show enterprise churn doubling",
            client_type=ClientType.HUMAN_UI,
        )
    )
    assert outcome.result.verdict is GateVerdict.REFUSED_INSIDER
    assert proposer.calls == 0  # the model was never invoked
    with session_factory() as s:
        assert s.scalars(select(ThesisAnalysis)).all() == []
