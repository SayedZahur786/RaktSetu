"""Decision layer: the questions RaktSetu asks, and the OpenAI Decisions API adapter.

This is the ONLY file that knows the Decisions API request and response format.
Everything else sees provider-agnostic QuestionSpec in and Verdict out, so another
decision model can plug in by implementing the `Decider` protocol.

Verified against openai-python 3.28.0 (`client.decisions.create(model, input, questions)`):
  questions: {"type": "predicate"|"choice"|"score", "name", "instructions",
              "choices": [{"value", "description"}]   (choice)
              "levels":  [{"label", "description"}]   (score, ordered, index = level)}
  answers:   {"type": "predicate", "name", "probability"}
             {"type": "choice", "name", "choice", "confidence", "probabilities": [{"value", "probability"}]}
             {"type": "score", "name", "score", "confidence", "probabilities": [{"label", "value", "probability"}]}
             {"type": "refusal", "name"}
"""
from __future__ import annotations

import math
import threading
from typing import Any, Protocol

from openai import OpenAI

from .config import DECISION_MODEL, openai_api_key
from .models import Answer, BloodGroup, QuestionSpec, Verdict
from .replay import ReplayStore

# ───────────────────────────── questions (provider-agnostic) ─────────────────────────────

URGENCY_LEVELS: list[tuple[str, str]] = [
    ("routine restock", "Stock replenishment or standing order; no patient currently waiting."),
    ("planned / elective", "A scheduled procedure or transfusion that can wait a day or more."),
    ("urgent", "A patient needs blood within hours but is not in immediate danger."),
    ("massive transfusion or life-threatening",
     "Active haemorrhage, massive transfusion protocol, or a patient who may die without blood now."),
]

REQUEST_QUESTIONS: list[QuestionSpec] = [
    QuestionSpec(name="urgency", kind="score", options=URGENCY_LEVELS,
                 instructions="How urgent is the clinical need behind this blood request, judged from the hospital's note?"),
    QuestionSpec(name="note_ambiguous", kind="predicate",
                 instructions="The request cannot be acted on safely because the blood group requested, the number "
                              "of units, or what the blood is for is missing or unclear. A request that states all "
                              "three clearly is not ambiguous, even if it asks for a group the patients may not need."),
]

RESERVE_CHECK = QuestionSpec(
    name="reserve_check", kind="choice",
    instructions="After this dispatch, is the bank's remaining stock of the dispensed group adequate for the pending "
                 "orders described at this bank?",
    options=[
        ("safe", "Remaining stock comfortably covers known pending needs."),
        ("tight", "Remaining stock is low but no described patient is left without blood."),
        ("unsafe", "Dispatch would leave a described pending patient without blood."),
    ])

PRIORITY_CHECK = QuestionSpec(
    name="priority_check", kind="choice",
    instructions="Should this request be served from this bank now, or deferred because a pending order at this bank "
                 "has a more urgent clinical need for the same blood group?",
    options=[
        ("proceed", "No pending order at this bank has a more urgent need for the same group."),
        ("defer", "A pending order at this bank has a more urgent need for the same group."),
    ])


def substitution_question(substitute: BloodGroup, requested: BloodGroup) -> QuestionSpec:
    return QuestionSpec(
        name="substitution_ok", kind="predicate",
        instructions=f"The note says the patients who will receive this blood are Rh-positive (or of a group that "
                     f"can receive {substitute}), so {substitute} red cells could be given instead of the {requested} "
                     f"requested.")


def candidate_questions() -> list[QuestionSpec]:
    """Per-bank questions. substitution_ok is about the note, not the bank, so the router asks it
    once per request (see router.Router._judge) instead of once per bank."""
    return [RESERVE_CHECK, PRIORITY_CHECK]


# ───────────────────────────── adapter interface ─────────────────────────────

class Decider(Protocol):
    model: str

    def ask(self, input_text: str, questions: list[QuestionSpec]) -> Verdict:
        """Answer every question about `input_text`. May raise; callers fail closed."""
        ...


class OutageDecider:
    """Simulates the decision API being down (`--simulate-outage`). Every call raises."""

    model = DECISION_MODEL

    def ask(self, input_text: str, questions: list[QuestionSpec]) -> Verdict:
        raise ConnectionError("simulated Decisions API outage")


# ───────────────────────────── OpenAI wire format ─────────────────────────────

def to_wire(q: QuestionSpec) -> dict[str, Any]:
    wire: dict[str, Any] = {"type": q.kind, "name": q.name, "instructions": q.instructions}
    if q.kind == "choice":
        wire["choices"] = [{"value": v, "description": d} for v, d in q.options]
    elif q.kind == "score":
        wire["levels"] = [{"label": label, "description": d} for label, d in q.options]
    return wire


def _num(raw: dict[str, Any], key: str, upper: float | None = 1.0) -> float:
    """A finite number in [0, upper]. NaN, infinities and out-of-range values raise (fail closed)."""
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"answer field {key!r} missing or not numeric: {value!r}")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (upper is not None and number > upper):
        raise ValueError(f"answer field {key!r} out of range: {value!r}")
    return number


def parse_answer(raw: dict[str, Any], expected: QuestionSpec) -> Answer:
    kind = raw.get("type")
    if kind == "refusal":
        return Answer(name=expected.name, kind="refusal")
    if kind != expected.kind:
        raise ValueError(f"question {expected.name!r} expected {expected.kind}, got {kind!r}")
    if kind == "predicate":
        return Answer(name=expected.name, kind="predicate", probability=_num(raw, "probability"))
    probs_raw = raw.get("probabilities")
    if not isinstance(probs_raw, list):
        raise ValueError(f"question {expected.name!r}: probabilities missing")
    if kind == "choice":
        choice = raw.get("choice")
        if choice is None:
            raise ValueError(f"question {expected.name!r}: choice missing")
        return Answer(name=expected.name, kind="choice", choice=str(choice), confidence=_num(raw, "confidence"),
                      probabilities={str(p["value"]): _num(p, "probability") for p in probs_raw})
    return Answer(name=expected.name, kind="score", score=_num(raw, "score", upper=len(expected.options) - 1),
                  confidence=_num(raw, "confidence"),
                  probabilities={str(p["label"]): _num(p, "probability") for p in probs_raw})


def parse_decision(raw: dict[str, Any], questions: list[QuestionSpec]) -> dict[str, Answer]:
    """Map a raw decisions response to answers by question name.

    Answers are matched by `name`; an unnamed answer falls back to its position. A
    question with no answer is simply absent, which policy.py treats as fail-closed.
    Raises ValueError on a malformed answer.
    """
    answers_raw = raw.get("answers")
    if not isinstance(answers_raw, list):
        raise ValueError("decision response has no answers list")
    by_name = {q.name: q for q in questions}
    parsed: dict[str, Answer] = {}
    for i, a in enumerate(answers_raw):
        if not isinstance(a, dict):
            raise ValueError(f"answer {i} is not an object")
        name = a.get("name") or (questions[i].name if i < len(questions) else None)
        if name in by_name:
            parsed[name] = parse_answer(a, by_name[name])
    return parsed


class OpenAIDecider:
    """Decisions API adapter with record/replay. Raises on any failure; the router fails closed."""

    def __init__(self, store: ReplayStore, model: str = DECISION_MODEL, timeout_s: float = 30.0) -> None:
        self.model = model
        self.store = store
        self.timeout_s = timeout_s
        self._client: OpenAI | None = None
        self._lock = threading.Lock()

    def _get_client(self) -> OpenAI:
        with self._lock:
            if self._client is None:
                key = openai_api_key()
                if not key:
                    raise RuntimeError("OPENAI_API_KEY is not set (set RAKTSETU_REPLAY=1 to use recorded fixtures)")
                self._client = OpenAI(api_key=key, timeout=self.timeout_s, max_retries=1)
            return self._client

    def ask(self, input_text: str, questions: list[QuestionSpec]) -> Verdict:
        wire_questions = [to_wire(q) for q in questions]
        payload = {"api": "decisions.create", "model": self.model, "input": input_text, "questions": wire_questions}

        def live() -> dict[str, Any]:
            resp = self._get_client().decisions.create(model=self.model, input=input_text, questions=wire_questions)
            return resp.model_dump(mode="json")

        raw, latency_ms = self.store.call(payload, live)
        usage = raw.get("usage") or {}
        return Verdict(answers=parse_decision(raw, questions), model=raw.get("model"),
                       latency_ms=round(latency_ms, 1), input_tokens=usage.get("input_tokens"))
