"""GPT-6.1 Sol writer (Responses API): bank request draft, officer explanation, follow-up question.

The only file that knows the Responses API format. Sol receives the finished decision and
can only explain it. If Sol fails for any reason, a template draft is used and the decision
is still shown.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from openai import OpenAI

from .config import WRITER_MODEL, WRITER_REASONING_EFFORT, openai_api_key
from .models import Candidate, Draft, Recommendation
from .replay import FixtureMissing, ReplayStore

log = logging.getLogger(__name__)

INSTRUCTIONS = """You write for RaktSetu, a blood request router used by blood bank officers in India.
The routing decision below is FINAL. It was made by code from typed model judgments and fixed thresholds.
You cannot change it, add candidates, or second-guess it; you only explain it and draft text.

Return JSON with exactly these fields:
- bank_request: a short, formal request to the chosen bank (requested group, units, urgency, requesting
  hospital). If no bank was chosen, write one sentence saying no request is being sent and why.
- officer_explanation: 2-3 sentences for the officer citing the verdicts and computed facts given
  (stock now/after, reserve and priority verdicts, substitution if any). State that an officer must confirm.
- follow_up_question: if status is HOLD_FOR_HUMAN, the single most useful question to ask the requesting
  hospital (for an unclear note: which blood group, how many units, and for what clinical reason).
  Otherwise null.
Plain text inside each field. No markdown. Do not invent facts not present in the input."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "bank_request": {"type": "string"},
        "officer_explanation": {"type": "string"},
        "follow_up_question": {"type": ["string", "null"]},
    },
    "required": ["bank_request", "officer_explanation", "follow_up_question"],
}


def _verdict_summary(c: Candidate) -> dict[str, str]:
    if c.verdict is None or c.verdict.error:
        return {}
    out: dict[str, str] = {}
    for name, a in c.verdict.answers.items():
        if a.kind == "choice":
            out[name] = f"{a.choice} (p={a.probabilities.get(a.choice or '', 0.0):.2f})"
        elif a.kind == "predicate":
            out[name] = f"p(yes)={a.probability:.2f}"
        else:
            out[name] = a.kind
    return out


def decision_brief(rec: Recommendation) -> dict[str, Any]:
    """The structured result Sol sees: labelled facts and verdicts, never bare numbers."""
    def cand(c: Candidate) -> dict[str, Any]:
        return {"bank": c.bank, "dispensed_group": c.blood_group, "exact_match": c.exact,
                "units_now": c.units_now, "units_after_dispatch": c.units_after, "status": c.status,
                "reasons": c.reasons, "verdicts": _verdict_summary(c), "pending_at_bank": c.pending_summary}
    return {
        "request": {"id": rec.request.id, "note": rec.request.note, "blood_group": rec.request.blood_group,
                    "units": rec.request.units, "hospital": rec.request.hospital},
        "status": rec.status, "status_reasons": rec.reasons, "urgency": rec.urgency_label,
        "chosen": cand(rec.chosen) if rec.chosen else None,
        "other_candidates": [cand(c) for c in rec.candidates if c is not rec.chosen and c.status != "NO_STOCK"][:4],
        "data_source": rec.data_source,
    }


def template_draft(rec: Recommendation) -> Draft:
    """Deterministic fallback when Sol is unavailable."""
    req = rec.request
    hospital = req.hospital or "the requesting hospital"
    if rec.status == "ROUTED" and rec.chosen:
        c = rec.chosen
        sub = "" if c.exact else f" ({c.blood_group} as a substitute for {req.blood_group})"
        bank_request = (f"To {c.bank}: please prepare {req.units} units of {c.blood_group} packed red cells{sub} "
                        f"for {hospital}, request {req.id}. Urgency: {rec.urgency_label}.")
        explanation = (f"{c.bank} has {c.units_now} units of {c.blood_group}, leaving {c.units_after} after dispatch; "
                       f"reserve and priority checks passed. An officer must confirm before anything is dispatched.")
        follow_up = None
    else:
        bank_request = f"No request sent for {req.id}: status {rec.status}."
        explanation = (f"Request {req.id} is {rec.status}: {'; '.join(rec.reasons) or 'see candidate reasons'}. "
                       "An officer must review it.")
        follow_up = ("Please confirm the blood group, number of units, and the clinical reason for this request."
                     if rec.status == "HOLD_FOR_HUMAN" else None)
    return Draft(bank_request=bank_request, officer_explanation=explanation,
                 follow_up_question=follow_up, model=None, fallback=True)


class SolWriter:
    def __init__(self, store: ReplayStore, model: str = WRITER_MODEL, timeout_s: float = 30.0) -> None:
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
                    raise RuntimeError("OPENAI_API_KEY is not set")
                self._client = OpenAI(api_key=key, timeout=self.timeout_s, max_retries=1)
            return self._client

    def write(self, rec: Recommendation) -> Draft:
        """Sol's draft, or the template on any failure. FixtureMissing propagates (replay misconfiguration)."""
        brief = json.dumps(decision_brief(rec), ensure_ascii=False, sort_keys=True)
        text_format = {"format": {"type": "json_schema", "name": "raktsetu_draft", "schema": SCHEMA, "strict": True}}
        payload = {"api": "responses.create", "model": self.model, "instructions": INSTRUCTIONS, "input": brief,
                   "reasoning": {"effort": WRITER_REASONING_EFFORT}, "text": text_format}

        def live() -> dict[str, Any]:
            resp = self._get_client().responses.create(
                model=self.model, instructions=INSTRUCTIONS, input=brief,
                reasoning={"effort": WRITER_REASONING_EFFORT}, text=text_format)
            return {"model": resp.model, "output_text": resp.output_text,
                    "usage": resp.usage.model_dump(mode="json") if resp.usage else None}

        try:
            raw, _ = self.store.call(payload, live)
            data = json.loads(raw["output_text"])
            return Draft(bank_request=data["bank_request"], officer_explanation=data["officer_explanation"],
                         follow_up_question=data.get("follow_up_question"), model=raw.get("model"), fallback=False)
        except FixtureMissing:
            raise
        except Exception as ex:  # any Sol failure: keep the decision, use the template
            log.warning("writer failed (%s: %s); using template draft", type(ex).__name__, ex)
            return template_draft(rec)
