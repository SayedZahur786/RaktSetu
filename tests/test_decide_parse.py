import json

import pytest

from raktsetu import decide
from raktsetu.models import Recommendation, Request
from raktsetu.replay import FixtureMissing, ReplayStore, payload_key
from raktsetu.writer import SolWriter

QUESTIONS = decide.REQUEST_QUESTIONS + [decide.RESERVE_CHECK]

RAW = {
    "model": "gpt-6-luna",
    "usage": {"input_tokens": 406, "output_tokens": 0},
    "answers": [
        {"type": "score", "name": "urgency", "score": 2.8, "confidence": 0.91, "probabilities": [
            {"label": "routine restock", "value": 0, "probability": 0.0},
            {"label": "urgent", "value": 2, "probability": 0.2},
            {"label": "massive transfusion or life-threatening", "value": 3, "probability": 0.8}]},
        {"type": "predicate", "name": "note_ambiguous", "probability": 0.04},
        {"type": "choice", "name": "reserve_check", "choice": "safe", "confidence": 0.88, "probabilities": [
            {"value": "safe", "probability": 0.94}, {"value": "tight", "probability": 0.05},
            {"value": "unsafe", "probability": 0.01}]},
    ],
}


def test_to_wire_shapes():
    wire = [decide.to_wire(q) for q in QUESTIONS]
    assert wire[0]["type"] == "score" and [lv["label"] for lv in wire[0]["levels"]][-1].startswith("massive")
    assert set(wire[1]) == {"type", "name", "instructions"}
    assert [c["value"] for c in wire[2]["choices"]] == ["safe", "tight", "unsafe"]


def test_parse_all_answer_types():
    a = decide.parse_decision(RAW, QUESTIONS)
    assert a["urgency"].score == 2.8 and a["urgency"].probabilities["urgent"] == 0.2
    assert a["note_ambiguous"].probability == 0.04
    assert a["reserve_check"].choice == "safe" and a["reserve_check"].probabilities["unsafe"] == 0.01


def test_parse_refusal():
    raw = {"answers": [{"type": "refusal", "name": "urgency"}]}
    assert decide.parse_decision(raw, QUESTIONS)["urgency"].kind == "refusal"


def test_parse_unnamed_answers_fall_back_to_position():
    raw = {"answers": [{k: v for k, v in a.items() if k != "name"} for a in RAW["answers"]]}
    assert set(decide.parse_decision(raw, QUESTIONS)) == {"urgency", "note_ambiguous", "reserve_check"}


def test_parse_omitted_answer_is_absent():
    raw = {"answers": RAW["answers"][:1]}
    assert "reserve_check" not in decide.parse_decision(raw, QUESTIONS)


@pytest.mark.parametrize("raw", [
    {},
    {"answers": "nope"},
    {"answers": [{"type": "predicate", "name": "note_ambiguous"}]},                  # probability missing
    {"answers": [{"type": "predicate", "name": "note_ambiguous", "probability": "high"}]},
    {"answers": [{"type": "choice", "name": "reserve_check", "confidence": 0.9, "probabilities": []}]},
    {"answers": [{"type": "predicate", "name": "reserve_check", "probability": 0.9}]},  # wrong type
])
def test_parse_malformed_raises(raw):
    with pytest.raises(ValueError):
        decide.parse_decision(raw, QUESTIONS)


def test_substitution_question_only_when_asked():
    assert [q.name for q in decide.candidate_questions(None, "O-")] == ["reserve_check", "priority_check"]
    qs = decide.candidate_questions("O+", "O-")
    assert qs[-1].name == "substitution_ok" and "O+" in qs[-1].instructions


def test_replay_round_trip_is_deterministic(tmp_path):
    calls = []
    live = lambda: calls.append(1) or {"ok": True}  # noqa: E731
    payload = {"api": "decisions.create", "input": "x"}
    rec_resp, _ = ReplayStore(tmp_path, replay=False).call(payload, live)
    replay = ReplayStore(tmp_path, replay=True)
    assert replay.call(payload, live)[0] == rec_resp == replay.call(payload, live)[0]
    assert calls == [1]                                    # replay never calls live
    assert (tmp_path / f"{payload_key(payload)}.json").exists()


def test_replay_key_ignores_dict_order():
    assert payload_key({"a": 1, "b": 2}) == payload_key({"b": 2, "a": 1})


def test_replay_missing_fixture_raises_clearly(tmp_path):
    with pytest.raises(FixtureMissing, match="Run once live"):
        ReplayStore(tmp_path, replay=True).call({"api": "decisions.create"}, lambda: {})


def test_openai_decider_replays_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = ReplayStore(tmp_path, replay=False)
    store.call({"api": "decisions.create", "model": "gpt-6-luna", "input": "facts",
                "questions": [decide.to_wire(q) for q in QUESTIONS]}, lambda: RAW)
    verdict = decide.OpenAIDecider(ReplayStore(tmp_path, replay=True)).ask("facts", QUESTIONS)
    assert verdict.model == "gpt-6-luna" and verdict.input_tokens == 406
    assert verdict.answers["reserve_check"].choice == "safe"


def test_openai_decider_without_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        decide.OpenAIDecider(ReplayStore(tmp_path, replay=False)).ask("facts", QUESTIONS)


def test_writer_falls_back_to_template_on_bad_output(tmp_path):
    class BadStore(ReplayStore):
        def call(self, payload, live):
            return {"model": "gpt-6.1-sol", "output_text": "not json"}, 1.0
    rec = Recommendation(request=Request(id="r1", note="need blood"), status="HOLD_FOR_HUMAN",
                         reasons=["note judged ambiguous"], data_source="snapshot")
    draft = SolWriter(BadStore(tmp_path, replay=True)).write(rec)
    assert draft.fallback and draft.follow_up_question
    assert "HOLD_FOR_HUMAN" in draft.officer_explanation


def test_writer_parses_sol_json(tmp_path):
    out = {"bank_request": "To X", "officer_explanation": "Because Y.", "follow_up_question": None}
    class GoodStore(ReplayStore):
        def call(self, payload, live):
            assert "cannot change" in payload["instructions"]
            return {"model": "gpt-6.1-sol", "output_text": json.dumps(out)}, 1.0
    rec = Recommendation(request=Request(id="r1", note="n"), status="NO_STOCK", data_source="snapshot")
    draft = SolWriter(GoodStore(tmp_path, replay=True)).write(rec)
    assert not draft.fallback and draft.bank_request == "To X" and draft.model == "gpt-6.1-sol"
