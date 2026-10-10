from conftest import bank_verdict, choice, predicate, refusal, urgency_verdict

from raktsetu import policy as pol
from raktsetu.decide import URGENCY_LEVELS
from raktsetu.models import Candidate, Verdict

LEVELS = [lvl for lvl, _ in URGENCY_LEVELS]


# ── request gate ──────────────────────────────────────────────────────────────

def test_gate_passes_clear_confident_request(policy):
    assert pol.request_gate(urgency_verdict(), policy) == (True, [])


def test_gate_holds_ambiguous_note(policy):
    ok, reasons = pol.request_gate(urgency_verdict(ambiguous=policy.ambiguous_hold_p), policy)
    assert not ok and "ambiguous" in reasons[0]


def test_gate_holds_low_urgency_confidence(policy):
    ok, reasons = pol.request_gate(urgency_verdict(confidence=policy.urgency_min_confidence - 0.01), policy)
    assert not ok and "confidence" in reasons[0]


def test_gate_holds_on_error_missing_and_refusal(policy):
    assert pol.request_gate(None, policy)[0] is False
    assert pol.request_gate(Verdict(error="Timeout"), policy)[0] is False
    v = urgency_verdict()
    del v.answers["note_ambiguous"]
    assert pol.request_gate(v, policy)[0] is False
    v = urgency_verdict()
    v.answers["urgency"] = refusal("urgency")
    assert pol.request_gate(v, policy) == (False, ["urgency: model refused to answer"])


def test_gate_holds_when_probability_missing(policy):
    v = urgency_verdict()
    v.answers["note_ambiguous"].probability = None
    assert pol.request_gate(v, policy)[0] is False


def test_urgency_label_rounds_to_nearest_level():
    assert pol.urgency_label(urgency_verdict(level=2.6), LEVELS) == LEVELS[3]
    assert pol.urgency_label(urgency_verdict(level=0.2), LEVELS) == LEVELS[0]
    assert pol.urgency_label(Verdict(error="x"), LEVELS) is None


# ── candidate status ──────────────────────────────────────────────────────────

def test_recommend_when_every_check_passes(policy):
    assert pol.candidate_status(bank_verdict(), False, policy) == ("RECOMMEND", ["all checks passed"])


def test_unsafe_reserve_blocks(policy):
    status, reasons = pol.candidate_status(bank_verdict(safe=0.1, unsafe=policy.unsafe_block_p), False, policy)
    assert status == "BLOCKED" and "unsafe" in reasons[0]


def test_defer_blocks(policy):
    status, reasons = pol.candidate_status(bank_verdict(proceed=1 - policy.defer_block_p), False, policy)
    assert status == "BLOCKED" and "defer" in reasons[0]


def test_tight_reserve_holds(policy):
    v = bank_verdict()
    v.answers["reserve_check"] = choice("reserve_check", {"safe": 0.2, "tight": 0.7, "unsafe": 0.1})
    status, reasons = pol.candidate_status(v, False, policy)
    assert status == "HOLD_FOR_HUMAN" and "tight" in reasons[0]


def test_safe_below_threshold_holds(policy):
    status, _ = pol.candidate_status(bank_verdict(safe=policy.reserve_safe_min_p - 0.01), False, policy)
    assert status == "HOLD_FOR_HUMAN"


def test_low_proceed_holds(policy):
    status, _ = pol.candidate_status(bank_verdict(proceed=policy.priority_proceed_min_p - 0.01), False, policy)
    assert status == "HOLD_FOR_HUMAN"


def test_substitution_thresholds(policy):
    assert pol.candidate_status(bank_verdict(subst=policy.substitution_min_p), True, policy)[0] == "RECOMMEND"
    assert pol.candidate_status(bank_verdict(subst=policy.substitution_min_p - 0.01), True, policy)[0] == "HOLD_FOR_HUMAN"
    assert pol.candidate_status(bank_verdict(subst=policy.substitution_block_p - 0.01), True, policy)[0] == "BLOCKED"


def test_substitution_answer_missing_holds(policy):
    assert pol.candidate_status(bank_verdict(), True, policy)[0] == "HOLD_FOR_HUMAN"


def test_refusal_error_and_missing_hold(policy):
    v = bank_verdict()
    v.answers["priority_check"] = refusal("priority_check")
    assert pol.candidate_status(v, False, policy)[0] == "HOLD_FOR_HUMAN"
    assert pol.candidate_status(Verdict(error="APITimeoutError"), False, policy)[0] == "HOLD_FOR_HUMAN"
    assert pol.candidate_status(Verdict(answers={}), False, policy)[0] == "HOLD_FOR_HUMAN"
    assert pol.candidate_status(None, False, policy)[0] == "HOLD_FOR_HUMAN"


def test_wrong_answer_type_holds(policy):
    v = bank_verdict()
    v.answers["reserve_check"] = predicate("reserve_check", 0.99)
    assert pol.candidate_status(v, False, policy)[0] == "HOLD_FOR_HUMAN"


def test_blocked_wins_over_hold(policy):
    v = bank_verdict(unsafe=0.9, safe=0.05)
    v.answers["priority_check"] = refusal("priority_check")
    assert pol.candidate_status(v, False, policy)[0] == "BLOCKED"


# ── ranking and request status ────────────────────────────────────────────────

def cand(bank: str, status: str, exact: bool = True, after: int = 5, distance: float | None = None) -> Candidate:
    return Candidate(bank=bank, blood_group="O-", exact=exact, units_now=after + 2, units_after=after,
                     pending_summary="none", status=status, distance_km=distance)  # type: ignore[arg-type]


def test_rank_orders_status_exact_units_distance():
    ranked = pol.rank([
        cand("blocked", "BLOCKED"), cand("sub", "RECOMMEND", exact=False, after=50),
        cand("few", "RECOMMEND", after=2), cand("many", "RECOMMEND", after=9),
        cand("hold", "HOLD_FOR_HUMAN"), cand("none", "NO_STOCK"),
    ])
    assert [c.bank for c in ranked] == ["many", "few", "sub", "hold", "blocked", "none"]


def test_rank_uses_distance_only_as_tiebreak():
    ranked = pol.rank([cand("far", "RECOMMEND", distance=20), cand("near", "RECOMMEND", distance=3)])
    assert [c.bank for c in ranked] == ["near", "far"]


def test_request_status():
    assert pol.request_status(True, [cand("a", "RECOMMEND")]) == "ROUTED"
    assert pol.request_status(False, [cand("a", "RECOMMEND")]) == "HOLD_FOR_HUMAN"
    assert pol.request_status(True, [cand("a", "BLOCKED")]) == "HOLD_FOR_HUMAN"
    assert pol.request_status(True, [cand("a", "NO_STOCK")]) == "NO_STOCK"
    assert pol.request_status(True, []) == "NO_STOCK"
