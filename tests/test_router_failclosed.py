import json

import openai
import pytest
from conftest import PENDING, ScriptedDecider, bank_verdict, stock, urgency_verdict

from raktsetu.audit import AuditLog
from raktsetu.models import Answer, Verdict
from raktsetu.replay import FixtureMissing
from raktsetu.router import Router, parse_request

STOCK = stock(("Bank A", "O-", 6), ("Bank B", "O-", 12), ("Bank C", "O-", 1), ("Bank D", "O+", 40))
PPH = "2 units O-negative, massive transfusion, post-partum haemorrhage"


def route(decider, policy, note=PPH, tmp_path=None, snapshot=STOCK):
    audit = AuditLog(tmp_path / "audit.jsonl") if tmp_path else None
    router = Router(decider=decider, policy=policy, pending=PENDING, writer=None, audit=audit)
    return router.route(parse_request("r1", note), snapshot)


def test_routes_and_ranks_when_everything_passes(policy):
    rec = route(ScriptedDecider(urgency_verdict()), policy)
    assert rec.status == "ROUTED"
    assert rec.chosen.bank == "Bank B"                       # most units left after dispatch
    assert {c.bank: c.status for c in rec.candidates}["Bank C"] == "NO_STOCK"
    assert rec.draft and rec.draft.fallback                  # no writer -> template


@pytest.mark.parametrize("failure", [
    ConnectionError("down"),
    TimeoutError("timed out"),
    openai.APITimeoutError(request=None),  # type: ignore[arg-type]
    ValueError("decision response has no answers list"),
    RuntimeError("OPENAI_API_KEY is not set"),
])
def test_request_level_failure_holds_and_skips_bank_calls(policy, failure):
    decider = ScriptedDecider(failure)
    rec = route(decider, policy)
    assert rec.status == "HOLD_FOR_HUMAN"
    assert rec.chosen is None
    assert "decision call failed" in rec.reasons[0]
    assert all(c.status in ("HOLD_FOR_HUMAN", "NO_STOCK") for c in rec.candidates)
    assert decider.calls == [["urgency", "note_ambiguous"]]


def test_bank_level_failure_holds_never_recommends(policy):
    rec = route(ScriptedDecider(urgency_verdict(), bank=lambda t, n: TimeoutError("slow")), policy)
    assert rec.status == "HOLD_FOR_HUMAN"
    assert not any(c.status == "RECOMMEND" for c in rec.candidates)


def test_refusal_on_any_bank_question_holds(policy):
    def bank(text, names):
        v = bank_verdict()
        v.answers["reserve_check"] = Answer(name="reserve_check", kind="refusal")
        return v
    rec = route(ScriptedDecider(urgency_verdict(), bank=bank), policy)
    assert rec.status == "HOLD_FOR_HUMAN"


def test_refusal_on_urgency_holds(policy):
    v = urgency_verdict()
    v.answers["urgency"] = Answer(name="urgency", kind="refusal")
    assert route(ScriptedDecider(v), policy).status == "HOLD_FOR_HUMAN"


def test_missing_answer_holds(policy):
    rec = route(ScriptedDecider(urgency_verdict(), bank=lambda t, n: Verdict(answers={})), policy)
    assert rec.status == "HOLD_FOR_HUMAN"


def test_unparseable_note_holds_even_if_model_is_confident(policy):
    rec = route(ScriptedDecider(urgency_verdict(ambiguous=0.0)), policy, note="need blood asap pls")
    assert rec.status == "HOLD_FOR_HUMAN"
    assert any("blood group not stated" in r for r in rec.reasons)
    assert rec.candidates == []


def test_no_stock_anywhere(policy):
    rec = route(ScriptedDecider(urgency_verdict()), policy, snapshot=stock(("Bank C", "O-", 1), ("Bank D", "O+", 1)))
    assert rec.status == "NO_STOCK"
    assert [c.status for c in rec.candidates] == ["NO_STOCK", "NO_STOCK"]


def test_compatible_substitute_used_when_exact_group_absent(policy):
    rec = route(ScriptedDecider(urgency_verdict()), policy, note="3 units AB-negative for surgery")
    assert rec.status == "ROUTED" and rec.chosen.blood_group == "O-" and not rec.chosen.needs_permission


def test_blocked_exact_group_falls_back_to_permitted_substitute(policy):
    def bank(text, names):
        if "substitution_ok" in names:
            return bank_verdict(subst=0.95)
        return bank_verdict(safe=0.0, unsafe=1.0)
    rec = route(ScriptedDecider(urgency_verdict(level=0), bank=bank), policy,
                note="routine restock 5 units O-negative, ward patients typed Rh-positive")
    assert rec.status == "ROUTED"
    assert rec.chosen.blood_group == "O+" and not rec.chosen.exact
    blocked = [c for c in rec.candidates if c.status == "BLOCKED"]
    assert {c.bank for c in blocked} == {"Bank A", "Bank B"}  # blocked banks stay visible


def test_substitute_not_permitted_is_never_recommended(policy):
    def bank(text, names):
        if "substitution_ok" in names:
            return bank_verdict(subst=0.05)
        return bank_verdict(safe=0.0, unsafe=1.0)
    rec = route(ScriptedDecider(urgency_verdict(), bank=bank), policy)
    assert rec.status == "HOLD_FOR_HUMAN"
    assert all(c.status != "RECOMMEND" for c in rec.candidates)


def test_fixture_missing_is_not_swallowed(policy):
    with pytest.raises(FixtureMissing):
        route(ScriptedDecider(FixtureMissing("no fixture")), policy)


def test_decision_model_sees_computed_facts(policy):
    seen: list[str] = []
    def bank(text, names):
        seen.append(text)
        return bank_verdict()
    route(ScriptedDecider(urgency_verdict(), bank=bank), policy)
    bank_a = next(t for t in seen if "Candidate bank: Bank A." in t)
    assert "units at this bank now: 6. Units remaining after this dispatch: 4." in bank_a
    assert "pending orders at this bank: 6" in bank_a


def test_audit_logs_request_and_every_candidate(policy, tmp_path):
    rec = route(ScriptedDecider(urgency_verdict()), policy, tmp_path=tmp_path)
    lines = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert lines[0]["event"] == "REQUEST_DECISION" and lines[0]["status"] == rec.status
    assert len(lines) == 1 + len(rec.candidates)
    judged = [x for x in lines[1:] if x["verdict"]]
    assert judged and "probabilities" in judged[0]["verdict"]["answers"]["reserve_check"]
    assert all(x["data_source"].startswith("snapshot") for x in lines)


def test_outage_reason_reaches_audit_log(policy, tmp_path):
    route(ScriptedDecider(ConnectionError("simulated Decisions API outage")), policy, tmp_path=tmp_path)
    first = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert first["status"] == "HOLD_FOR_HUMAN"
    assert "simulated Decisions API outage" in first["reasons"][0]
