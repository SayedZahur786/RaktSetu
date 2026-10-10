"""Shared test helpers: verdict builders and a scripted decider. No network."""
from __future__ import annotations

from collections.abc import Callable

import pytest

from raktsetu.config import load_policy
from raktsetu.models import Answer, BankStock, PendingOrder, Policy, QuestionSpec, StockSnapshot, Verdict


@pytest.fixture
def policy() -> Policy:
    return load_policy()


def choice(name: str, probs: dict[str, float], confidence: float = 0.9) -> Answer:
    return Answer(name=name, kind="choice", choice=max(probs, key=lambda k: probs[k]), probabilities=probs,
                  confidence=confidence)


def predicate(name: str, p: float) -> Answer:
    return Answer(name=name, kind="predicate", probability=p)


def refusal(name: str) -> Answer:
    return Answer(name=name, kind="refusal")


def urgency_verdict(level: float = 3.0, confidence: float = 0.9, ambiguous: float = 0.05) -> Verdict:
    return Verdict(model="test", answers={
        "urgency": Answer(name="urgency", kind="score", score=level, confidence=confidence, probabilities={}),
        "note_ambiguous": predicate("note_ambiguous", ambiguous),
    })


def bank_verdict(safe: float = 0.95, unsafe: float = 0.0, proceed: float = 0.95,
                 subst: float | None = None) -> Verdict:
    tight = max(0.0, 1 - safe - unsafe)
    answers = {
        "reserve_check": choice("reserve_check", {"safe": safe, "tight": tight, "unsafe": unsafe}),
        "priority_check": choice("priority_check", {"proceed": proceed, "defer": 1 - proceed}),
    }
    if subst is not None:
        answers["substitution_ok"] = predicate("substitution_ok", subst)
    return Verdict(model="test", answers=answers)


class ScriptedDecider:
    """Answers request questions with `request` and bank questions via `bank(input_text, names)`."""

    model = "scripted"

    def __init__(self, request: Verdict | Exception,
                 bank: Callable[[str, list[str]], Verdict | Exception] | None = None) -> None:
        self.request = request
        self.bank = bank or (lambda text, names: bank_verdict(subst=0.9 if "substitution_ok" in names else None))
        self.calls: list[list[str]] = []

    def ask(self, input_text: str, questions: list[QuestionSpec]) -> Verdict:
        names = [q.name for q in questions]
        self.calls.append(names)
        out = self.request if "urgency" in names else self.bank(input_text, names)
        if isinstance(out, Exception):
            raise out
        return out


def stock(*rows: tuple[str, str, int]) -> StockSnapshot:
    return StockSnapshot(source="snapshot", fetched_at="2026-09-26T05:19:57", region="Test District",
                         banks=[BankStock(name=n, blood_group=g, units=u) for n, g, u in rows])  # type: ignore[arg-type]


PENDING = [PendingOrder(bank="Bank A", blood_group="O-", units=6, note="trauma in theatre now")]
