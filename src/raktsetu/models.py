"""Pydantic models shared across module boundaries. No I/O, no provider wire formats."""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field

BloodGroup = Literal["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]
CandidateStatus = Literal["RECOMMEND", "HOLD_FOR_HUMAN", "BLOCKED", "NO_STOCK"]
RequestStatus = Literal["ROUTED", "HOLD_FOR_HUMAN", "NO_STOCK"]
AnswerKind = Literal["predicate", "choice", "score", "refusal"]
Prob = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Policy(BaseModel):
    """Thresholds from policy.yaml (loaded by config.py)."""

    urgency_min_confidence: float = Field(ge=0, le=1)
    ambiguous_hold_p: float = Field(ge=0, le=1)
    reserve_safe_min_p: float = Field(ge=0, le=1)
    unsafe_block_p: float = Field(ge=0, le=1)
    priority_proceed_min_p: float = Field(ge=0, le=1)
    defer_block_p: float = Field(ge=0, le=1)
    substitution_min_p: float = Field(ge=0, le=1)
    substitution_block_p: float = Field(ge=0, le=1)
    min_units_after_dispatch: int = Field(ge=0)
    hold_on_pending_shortfall: bool = True
    fail_mode: str = "closed"
    api_timeout_s: float = Field(gt=0)


class Request(BaseModel):
    """A hospital's blood request. `note` is free text; group/units are parsed in code."""

    id: str
    note: str
    blood_group: BloodGroup | None = None
    units: int | None = Field(default=None, gt=0)
    hospital: str | None = None


class PendingOrder(BaseModel):
    """An order already waiting at a bank (synthetic; from data/demo_requests.json)."""

    bank: str
    blood_group: BloodGroup
    units: int = Field(gt=0)
    note: str


class BankStock(BaseModel):
    name: str
    category: str | None = None
    blood_group: BloodGroup
    units: int = Field(ge=0)
    last_updated: str | None = None
    lat: float | None = None
    lon: float | None = None


class StockSnapshot(BaseModel):
    banks: list[BankStock]
    source: Literal["live", "snapshot"]
    fetched_at: str
    region: str
    groups: list[BloodGroup] = Field(default_factory=list)  # groups actually fetched; others are unknown, not 0
    fallback: bool = False                                  # live was requested but the snapshot was used

    @property
    def label(self) -> str:
        note = " (LIVE FETCH FAILED, fallback)" if self.fallback else ""
        return f"{self.source} e-RaktKosh, {self.region}, {self.fetched_at}{note}"


class QuestionSpec(BaseModel):
    """A provider-agnostic question. decide.py serialises it to the wire format."""

    name: str
    kind: Literal["predicate", "choice", "score"]
    instructions: str
    # choice: (value, description) pairs; score: ordered (label, description) levels.
    options: list[tuple[str, str]] = Field(default_factory=list)


class Answer(BaseModel):
    """One parsed answer. Fields not used by `kind` stay None/empty."""

    name: str
    kind: AnswerKind
    probability: Prob | None = None               # predicate
    choice: str | None = None                     # choice
    probabilities: dict[str, Prob] = Field(default_factory=dict)  # choice values / score labels
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)  # score: weighted level index
    confidence: Prob | None = None                # choice, score


class Verdict(BaseModel):
    """The result of one decision call. `error` set means the call failed and nothing else is trusted."""

    answers: dict[str, Answer] = Field(default_factory=dict)
    model: str | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    error: str | None = None


class Candidate(BaseModel):
    bank: str
    blood_group: BloodGroup                       # group that would be dispensed
    exact: bool                                   # dispensed group == requested group
    needs_permission: bool = False                # substitute valid only if the note permits it
    units_now: int
    units_after: int
    pending_units: int = 0                        # units already promised to pending orders at this bank
    pending_summary: str
    distance_km: float | None = None
    verdict: Verdict | None = None
    status: CandidateStatus = "HOLD_FOR_HUMAN"
    reasons: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    """Text written by the writer model. It explains the decision; it never changes it."""

    bank_request: str
    officer_explanation: str
    follow_up_question: str | None = None
    model: str | None = None
    fallback: bool = False


class Recommendation(BaseModel):
    decision_id: str = Field(default_factory=lambda: f"dec-{uuid.uuid4().hex[:12]}")  # unique per routing run
    request: Request
    status: RequestStatus
    reasons: list[str] = Field(default_factory=list)
    urgency: Verdict | None = None
    urgency_label: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    chosen: Candidate | None = None
    data_source: str
    draft: Draft | None = None
