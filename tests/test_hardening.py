"""Regression tests for the whole-branch review findings (each failed before its fix)."""
import json
import math

import pytest
from conftest import PENDING, ScriptedDecider, bank_verdict, predicate, stock, urgency_verdict
from pydantic import ValidationError

from raktsetu import decide, eraktkosh
from raktsetu import policy as pol
from raktsetu.audit import AuditLog
from raktsetu.models import Answer, StockSnapshot, Verdict
from raktsetu.router import Router, parse_request

PPH = "2 units O-negative, massive transfusion, post-partum haemorrhage"
RESTOCK = "routine restock 5 units O-negative, ward patients typed Rh-positive"


def route(decider, policy, note=PPH, snapshot=None, audit=None):
    snap = snapshot or stock(("Bank A", "O-", 6), ("Bank B", "O-", 12), ("Bank D", "O+", 40), ("Bank E", "O+", 30))
    return Router(decider=decider, policy=policy, pending=PENDING, audit=audit).route(parse_request("r1", note), snap)


# ── C1: malformed numbers must never pass a threshold ─────────────────────────

@pytest.mark.parametrize("bad", [math.nan, math.inf, -0.5, 1.5])
def test_out_of_range_probability_is_rejected_by_parser(bad):
    raw = {"answers": [{"type": "predicate", "name": "note_ambiguous", "probability": bad}]}
    with pytest.raises(ValueError):
        decide.parse_decision(raw, decide.REQUEST_QUESTIONS)


def test_out_of_range_confidence_is_rejected_by_parser():
    raw = {"answers": [{"type": "choice", "name": "reserve_check", "choice": "safe", "confidence": 7,
                        "probabilities": [{"value": "safe", "probability": 1.0}]}]}
    with pytest.raises(ValueError):
        decide.parse_decision(raw, [decide.RESERVE_CHECK])


def test_answer_model_rejects_nan():
    with pytest.raises(ValidationError):
        Answer(name="x", kind="predicate", probability=math.nan)


# ── C2: live stock requested but unavailable -> nothing is recommended ────────

def test_snapshot_fallback_is_flagged(monkeypatch):
    def boom(*a, **k):
        raise eraktkosh.requests.ConnectionError("down")
    monkeypatch.setattr(eraktkosh, "fetch_live", boom)
    snap = eraktkosh.load_stock(prefer_live=True)
    assert snap.source == "snapshot" and snap.fallback
    assert not eraktkosh.load_stock(prefer_live=False).fallback


def test_fallback_stock_holds_every_candidate(policy):
    snap = stock(("Bank B", "O-", 12)).model_copy(update={"fallback": True})
    rec = route(ScriptedDecider(urgency_verdict()), policy, snapshot=snap)
    assert rec.status == "HOLD_FOR_HUMAN"
    assert not any(c.status == "RECOMMEND" for c in rec.candidates)
    assert any("live stock" in r for r in rec.reasons)


# ── I1: groups that were never fetched are unknown, not zero ──────────────────

def test_unfetched_group_holds_instead_of_substituting(policy):
    rec = route(ScriptedDecider(urgency_verdict()), policy, note="3 units A-negative for elective CABG")
    assert rec.status == "HOLD_FOR_HUMAN"
    assert rec.candidates == []
    assert any("A- stock is not fetched" in r for r in rec.reasons)


# ── I2: pending-order shortfall is checked in code, not left to the model ─────

def test_policy_holds_when_dispatch_leaves_less_than_pending(policy):
    status, reasons = pol.candidate_status(bank_verdict(), False, policy, units_after=4, pending_units=6)
    assert status == "HOLD_FOR_HUMAN" and "pending" in reasons[0]


def test_router_holds_bank_with_pending_shortfall_even_if_model_says_safe(policy):
    rec = route(ScriptedDecider(urgency_verdict()), policy)   # Bank A: 6 O-, 6 pending
    bank_a = next(c for c in rec.candidates if c.bank == "Bank A")
    assert bank_a.status == "HOLD_FOR_HUMAN" and bank_a.pending_units == 6


# ── I3: ambiguous unit counts are not guessed ─────────────────────────────────

@pytest.mark.parametrize("note", [
    "have 2 units in hand, need 4 more units O-negative",
    "1-2 units O-negative",
    "2 to 3 units O-negative",
])
def test_ambiguous_units_are_none(note):
    assert parse_request("r", note).units is None


def test_single_unit_count_still_parses():
    assert parse_request("r", "O-neg x2 now").units == 2
    assert parse_request("r", "2 units O-negative, 2 units already crossmatched elsewhere").units == 2


# ── I4: substitution permission is asked once per request, not per bank ───────

def test_substitution_asked_once_and_applied_to_every_conditional_bank(policy):
    def bank(text, names):
        if names == ["substitution_ok"]:
            return Verdict(model="t", answers={"substitution_ok": predicate("substitution_ok", 0.05)})
        if "(exact match)" in text:
            return bank_verdict(safe=0.0, unsafe=1.0)
        return bank_verdict()
    decider = ScriptedDecider(urgency_verdict(level=0), bank=bank)
    rec = route(decider, policy, note=RESTOCK)
    assert sum("substitution_ok" in c for c in decider.calls) == 1
    subs = [c for c in rec.candidates if c.needs_permission]
    assert len(subs) == 2 and all(c.status == "BLOCKED" for c in subs)
    assert all(c.verdict.answers["substitution_ok"].probability == 0.05 for c in subs)


def test_substitution_call_failure_holds_conditional_banks(policy):
    def bank(text, names):
        if names == ["substitution_ok"]:
            return TimeoutError("slow")
        return bank_verdict(safe=0.0, unsafe=1.0) if "(exact match)" in text else bank_verdict()
    rec = route(ScriptedDecider(urgency_verdict(level=0), bank=bank), policy, note=RESTOCK)
    assert rec.status == "HOLD_FOR_HUMAN"
    assert all(c.status != "RECOMMEND" for c in rec.candidates)


# ── I5: every decision has its own id; confirmations record what was overridden

def test_identical_requests_get_distinct_decision_ids(policy):
    d = ScriptedDecider(urgency_verdict())
    assert route(d, policy).decision_id != route(d, policy).decision_id


def test_confirmation_records_status_and_override(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.record_confirmation("dec-1", "r1", "Bank B", "O-", 2, "Dr. X", "HOLD_FOR_HUMAN", ["reserve tight"])
    line = json.loads((tmp_path / "a.jsonl").read_text())
    assert line["decision_id"] == "dec-1" and line["candidate_status"] == "HOLD_FOR_HUMAN"
    assert line["override"] is True and line["candidate_reasons"] == ["reserve tight"]


# ── minor (secret hygiene): error text never carries key material ─────────────

def test_error_text_is_redacted(policy):
    leak = PermissionError("Incorrect API key provided: sk-proj-ABCdef123456******wxyz")
    rec = route(ScriptedDecider(leak), policy)
    assert "sk-proj-ABC" not in json.dumps(rec.model_dump())
    assert "[redacted]" in rec.reasons[0]


def test_snapshot_records_fetched_groups():
    assert set(eraktkosh.load_snapshot().groups) == {"O-", "O+"}
    assert isinstance(eraktkosh.load_snapshot(), StockSnapshot)
