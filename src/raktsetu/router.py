"""Orchestration: request -> candidate banks -> decisions -> policy -> ranking -> draft -> audit.

Stock arithmetic, compatibility and ranking happen here and in policy.py/compat.py. The
decision model only sees computed facts as text. Every failure path ends in HOLD_FOR_HUMAN.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from . import compat, policy as pol
from . import config
from .audit import AuditLog
from .config import DATA_DIR
from .decide import REQUEST_QUESTIONS, URGENCY_LEVELS, Decider, OpenAIDecider, OutageDecider, candidate_questions
from .eraktkosh import load_snapshot, load_stock
from .models import BloodGroup, Candidate, PendingOrder, Policy, QuestionSpec, Recommendation, Request, StockSnapshot, Verdict
from .replay import FixtureMissing, ReplayStore
from .writer import SolWriter, template_draft

log = logging.getLogger(__name__)

DEMO_FILE = DATA_DIR / "demo_requests.json"
MAX_PARALLEL_CALLS = 8
_UNITS_RE = re.compile(r"\b(\d{1,3})\s*(?:units?|u|bags?|pints?|PRBCs?|RBCs?)\b|[x×]\s*(\d{1,3})\b", re.IGNORECASE)


class Scenario(BaseModel):
    id: str
    title: str
    hospital: str
    note: str
    simulate_outage: bool = False
    expected_status: str


class DemoData(BaseModel):
    pending_orders: list[PendingOrder]
    scenarios: list[Scenario]


def load_demo(path: Path = DEMO_FILE) -> DemoData:
    return DemoData.model_validate(json.loads(path.read_text()))


def content_request_id(note: str, hospital: str | None) -> str:
    """Stable id for an ad-hoc request, so identical requests replay from the same fixtures."""
    digest = hashlib.sha256(f"{hospital or ''}\n{note.strip()}".encode("utf-8")).hexdigest()
    return f"req-{digest[:10]}"


def parse_request(request_id: str | None, note: str, hospital: str | None = None) -> Request:
    """Structured fields come from code. Unparseable fields stay None and force a hold."""
    request_id = request_id or content_request_id(note, hospital)
    m = _UNITS_RE.search(note)
    n = int(m.group(1) or m.group(2)) if m else 0
    units = n if n > 0 else None
    return Request(id=request_id, note=note.strip(), blood_group=compat.parse_group(note), units=units,
                   hospital=hospital)


def request_facts(req: Request) -> str:
    return (f"Blood request {req.id} from {req.hospital or 'a hospital'}.\n"
            f"Hospital note: \"{req.note}\"\n"
            f"Parsed by code from the note: blood group {req.blood_group or 'NOT STATED'}, "
            f"units {req.units if req.units is not None else 'NOT STATED'}.")


def pending_summary(pending: list[PendingOrder], bank: str, group: BloodGroup) -> tuple[int, str]:
    """Units already promised at `bank` for `group`, and a one-line summary of those orders."""
    same = [p for p in pending if p.bank == bank and p.blood_group == group]
    if not same:
        return 0, "none"
    return sum(p.units for p in same), " | ".join(f"{p.units}u {p.blood_group}: {p.note}" for p in same)


def candidate_facts(req: Request, c: Candidate, urgency: str | None, pending_units: int) -> str:
    match = "exact match" if c.exact else f"substitute for requested {req.blood_group}"
    return (f"Blood request {req.id} from {req.hospital or 'a hospital'}: {req.units} units of {req.blood_group} "
            f"packed red cells. Request urgency (judged earlier): {urgency or 'unknown'}.\n"
            f"Hospital note: \"{req.note}\"\n"
            f"Candidate bank: {c.bank}.\n"
            f"Group that would be dispensed: {c.blood_group} ({match}).\n"
            f"{c.blood_group} units at this bank now: {c.units_now}. "
            f"Units remaining after this dispatch: {c.units_after}.\n"
            f"Units of {c.blood_group} already requested by pending orders at this bank: {pending_units}.\n"
            f"Pending orders at this bank for {c.blood_group}: {c.pending_summary}")


@dataclass
class Router:
    decider: Decider
    policy: Policy
    pending: list[PendingOrder]
    writer: SolWriter | None = None
    audit: AuditLog | None = None

    def _ask(self, text: str, questions: list[QuestionSpec]) -> Verdict:
        """Fail-closed wrapper: any decider failure becomes a Verdict with `error` set."""
        try:
            return self.decider.ask(text, questions)
        except FixtureMissing:
            raise
        except Exception as ex:  # network, timeout, auth, malformed response: all hold
            log.warning("decision call failed: %s: %s", type(ex).__name__, ex)
            return Verdict(error=f"{type(ex).__name__}: {ex}", model=getattr(self.decider, "model", None))

    def _build(self, req: Request, stock: StockSnapshot, group: BloodGroup, exact: bool,
               needs_permission: bool) -> list[Candidate]:
        assert req.units is not None
        out: list[Candidate] = []
        for b in stock.banks:
            if b.blood_group != group or b.units <= 0:
                continue
            _, summary = pending_summary(self.pending, b.name, group)
            c = Candidate(bank=b.name, blood_group=group, exact=exact, needs_permission=needs_permission,
                          units_now=b.units, units_after=b.units - req.units, pending_summary=summary)
            if b.units < req.units:
                c.status, c.units_after = "NO_STOCK", b.units
                c.reasons = [f"only {b.units} unit(s) of {group}, {req.units} needed"]
            out.append(c)
        return out

    def _judge(self, req: Request, candidates: list[Candidate], urgency: str | None) -> None:
        todo = [c for c in candidates if c.status != "NO_STOCK"]

        def run(c: Candidate) -> Verdict:
            pending_units, _ = pending_summary(self.pending, c.bank, c.blood_group)
            sub = c.blood_group if c.needs_permission else None
            assert req.blood_group is not None
            return self._ask(candidate_facts(req, c, urgency, pending_units), candidate_questions(sub, req.blood_group))

        if not todo:
            return
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CALLS, len(todo))) as pool:
            verdicts = list(pool.map(run, todo))
        for c, v in zip(todo, verdicts):
            c.verdict = v
            c.status, c.reasons = pol.candidate_status(v, c.needs_permission, self.policy, c.units_after)

    def route(self, req: Request, stock: StockSnapshot) -> Recommendation:
        urgency_v = self._ask(request_facts(req), REQUEST_QUESTIONS)
        gate_ok, reasons = pol.request_gate(urgency_v, self.policy)
        label = pol.urgency_label(urgency_v, [lvl for lvl, _ in URGENCY_LEVELS])
        if req.blood_group is None:
            gate_ok, reasons = False, reasons + ["blood group not stated (or more than one) in the note"]
        if req.units is None:
            gate_ok, reasons = False, reasons + ["number of units not stated in the note"]

        candidates: list[Candidate] = []
        if req.blood_group is not None and req.units is not None:
            candidates = self._build(req, stock, req.blood_group, exact=True, needs_permission=False)
            if gate_ok:
                self._judge(req, candidates, label)
                if not any(c.status == "RECOMMEND" for c in candidates):
                    # No exact-group bank can be recommended: look at substitutes compat.py allows.
                    subs: list[Candidate] = []
                    for g in compat.safe_substitutes(req.blood_group):
                        subs += self._build(req, stock, g, exact=False, needs_permission=False)
                    for g in compat.conditional_substitutes(req.blood_group):
                        subs += self._build(req, stock, g, exact=False, needs_permission=True)
                    self._judge(req, subs, label)
                    candidates += subs
            else:
                for c in candidates:
                    if c.status != "NO_STOCK":
                        c.status, c.reasons = "HOLD_FOR_HUMAN", ["request held before per-bank checks"]

        ranked = pol.rank(candidates)
        status = pol.request_status(gate_ok, ranked)
        if status == "NO_STOCK" and not reasons:
            reasons = [f"no bank in {stock.region} holds {req.units} units of {req.blood_group} or a substitute"]
        if status == "HOLD_FOR_HUMAN" and gate_ok:
            reasons = reasons + ["no candidate bank passed every check"]
        chosen = next((c for c in ranked if c.status == "RECOMMEND"), None) if status == "ROUTED" else None
        rec = Recommendation(request=req, status=status, reasons=reasons, urgency=urgency_v, urgency_label=label,
                             candidates=ranked, chosen=chosen, data_source=stock.label)
        rec.draft = self.writer.write(rec) if self.writer else template_draft(rec)
        if self.audit:
            self.audit.record_decision(rec)
        return rec


def build_router(simulate_outage: bool = False, audit_path: Path | None = None) -> Router:
    """Wire the production router from env + policy.yaml. Replay mode needs no API key."""
    policy = config.load_policy()
    store = ReplayStore(config.FIXTURES_DIR, replay=config.replay_enabled())
    decider: Decider = OutageDecider() if simulate_outage else OpenAIDecider(store, timeout_s=policy.api_timeout_s)
    return Router(decider=decider, policy=policy, pending=load_demo().pending_orders,
                  writer=SolWriter(store, timeout_s=policy.api_timeout_s),
                  audit=AuditLog(audit_path or config.OUT_DIR / "audit.jsonl"))


def stock_for_run(prefer_live: bool) -> StockSnapshot:
    """Replay mode always uses the snapshot: live stock changes daily and would miss recorded fixtures."""
    if config.replay_enabled():
        return load_snapshot()
    state, district = config.region_codes()
    return load_stock(prefer_live, state, district)
