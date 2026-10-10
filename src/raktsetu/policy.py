"""Pure policy: verdicts + policy.yaml thresholds -> statuses, and candidate ranking. No I/O.

The decision model only judges. Every RECOMMEND / HOLD_FOR_HUMAN / BLOCKED call is made
here, in code, against thresholds that live in policy.yaml. Anything missing, refused or
errored resolves to HOLD_FOR_HUMAN (fail closed).
"""
from __future__ import annotations

from .models import Answer, Candidate, CandidateStatus, Policy, RequestStatus, Verdict


def _answer(verdict: Verdict, name: str, kind: str) -> tuple[Answer | None, str | None]:
    """The usable answer, or (None, reason-to-hold)."""
    a = verdict.answers.get(name)
    if a is None:
        return None, f"{name}: no answer returned"
    if a.kind == "refusal":
        return None, f"{name}: model refused to answer"
    if a.kind != kind:
        return None, f"{name}: unexpected answer type {a.kind}"
    return a, None


def urgency_label(verdict: Verdict | None, levels: list[str]) -> str | None:
    """Nearest level to the weighted urgency score, or None if unavailable."""
    if verdict is None or verdict.error:
        return None
    a = verdict.answers.get("urgency")
    if a is None or a.kind != "score" or a.score is None:
        return None
    idx = min(max(round(a.score), 0), len(levels) - 1)
    return levels[idx]


def request_gate(verdict: Verdict | None, policy: Policy) -> tuple[bool, list[str]]:
    """May this request proceed to per-bank checks? (ok, reasons)."""
    if verdict is None:
        return False, ["request judgment not available"]
    if verdict.error:
        return False, [f"decision call failed: {verdict.error}"]
    urgency, why = _answer(verdict, "urgency", "score")
    if urgency is None:
        return False, [why or "urgency unavailable"]
    ambiguous, why = _answer(verdict, "note_ambiguous", "predicate")
    if ambiguous is None:
        return False, [why or "note_ambiguous unavailable"]
    if ambiguous.probability is None:
        return False, ["note_ambiguous: probability missing"]
    reasons: list[str] = []
    if ambiguous.probability >= policy.ambiguous_hold_p:
        reasons.append(f"note judged ambiguous (p={ambiguous.probability:.2f} >= {policy.ambiguous_hold_p})")
    if urgency.confidence is None or urgency.confidence < policy.urgency_min_confidence:
        reasons.append(f"urgency confidence {urgency.confidence} below {policy.urgency_min_confidence}")
    return not reasons, reasons


def candidate_status(verdict: Verdict | None, needs_permission: bool,
                     policy: Policy) -> tuple[CandidateStatus, list[str]]:
    """Status for one bank with enough stock. BLOCKED wins over HOLD; RECOMMEND needs every check to pass."""
    if verdict is None:
        return "HOLD_FOR_HUMAN", ["not judged"]
    if verdict.error:
        return "HOLD_FOR_HUMAN", [f"decision call failed: {verdict.error}"]

    blocked: list[str] = []
    hold: list[str] = []

    reserve, why = _answer(verdict, "reserve_check", "choice")
    if reserve is None:
        hold.append(why or "reserve_check unavailable")
    else:
        p_unsafe = reserve.probabilities.get("unsafe", 0.0)
        p_safe = reserve.probabilities.get("safe", 0.0)
        if p_unsafe >= policy.unsafe_block_p:
            blocked.append(f"reserve_check unsafe (p={p_unsafe:.2f})")
        elif reserve.choice != "safe" or p_safe < policy.reserve_safe_min_p:
            hold.append(f"reserve_check {reserve.choice} (p(safe)={p_safe:.2f} < {policy.reserve_safe_min_p})"
                        if reserve.choice == "safe" else f"reserve_check {reserve.choice} (p(safe)={p_safe:.2f})")

    priority, why = _answer(verdict, "priority_check", "choice")
    if priority is None:
        hold.append(why or "priority_check unavailable")
    else:
        p_defer = priority.probabilities.get("defer", 0.0)
        p_proceed = priority.probabilities.get("proceed", 0.0)
        if p_defer >= policy.defer_block_p:
            blocked.append(f"priority_check defer (p={p_defer:.2f})")
        elif priority.choice != "proceed" or p_proceed < policy.priority_proceed_min_p:
            hold.append(f"priority_check {priority.choice} (p(proceed)={p_proceed:.2f})")

    if needs_permission:
        sub, why = _answer(verdict, "substitution_ok", "predicate")
        if sub is None:
            hold.append(why or "substitution_ok unavailable")
        elif sub.probability is None:
            hold.append("substitution_ok: probability missing")
        else:
            if sub.probability < policy.substitution_block_p:
                blocked.append(f"note does not permit substitution (p={sub.probability:.2f})")
            elif sub.probability < policy.substitution_min_p:
                hold.append(f"substitution permission uncertain (p={sub.probability:.2f})")

    if blocked:
        return "BLOCKED", blocked + hold
    if hold:
        return "HOLD_FOR_HUMAN", hold
    return "RECOMMEND", ["all checks passed"]


_STATUS_ORDER: dict[str, int] = {"RECOMMEND": 0, "HOLD_FOR_HUMAN": 1, "BLOCKED": 2, "NO_STOCK": 3}


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """RECOMMEND first (exact, then always-compatible substitutes, then substitutes that rely on
    the note's permission; most units left; distance when known), then HOLD_FOR_HUMAN,
    BLOCKED and NO_STOCK. Nothing is dropped."""
    def key(c: Candidate) -> tuple[int, int, int, float]:
        match = 0 if c.exact else (2 if c.needs_permission else 1)
        distance = c.distance_km if c.distance_km is not None else 0.0
        return (_STATUS_ORDER[c.status], match, -c.units_after, distance)
    return sorted(candidates, key=key)


def request_status(gate_ok: bool, candidates: list[Candidate]) -> RequestStatus:
    if any(c.status == "RECOMMEND" for c in candidates) and gate_ok:
        return "ROUTED"
    if not gate_ok:
        return "HOLD_FOR_HUMAN"
    if all(c.status == "NO_STOCK" for c in candidates):
        return "NO_STOCK"
    return "HOLD_FOR_HUMAN"
