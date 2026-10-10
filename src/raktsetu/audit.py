"""Append-only JSONL audit log (out/audit.jsonl): every decision and every officer confirmation."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .models import Candidate, Recommendation, Verdict


def _answers(verdict: Verdict | None) -> dict[str, Any] | None:
    if verdict is None:
        return None
    return {"answers": {k: v.model_dump(exclude_none=True) for k, v in verdict.answers.items()},
            "model": verdict.model, "latency_ms": verdict.latency_ms, "error": verdict.error}


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _append(self, record: dict[str, Any]) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def record_decision(self, rec: Recommendation) -> None:
        """One line for the request, then one per candidate, with raw probabilities and reasons."""
        self._append({
            "event": "REQUEST_DECISION", "request_id": rec.request.id, "status": rec.status, "reasons": rec.reasons,
            "blood_group": rec.request.blood_group, "units": rec.request.units, "urgency_label": rec.urgency_label,
            "urgency": _answers(rec.urgency), "data_source": rec.data_source,
            "chosen": rec.chosen.bank if rec.chosen else None,
            "writer_model": rec.draft.model if rec.draft else None,
            "writer_fallback": rec.draft.fallback if rec.draft else None,
        })
        for c in rec.candidates:
            self._append(self._candidate_record(rec, c))

    def _candidate_record(self, rec: Recommendation, c: Candidate) -> dict[str, Any]:
        return {"event": "CANDIDATE_DECISION", "request_id": rec.request.id, "bank": c.bank,
                "blood_group": c.blood_group, "exact": c.exact, "units_now": c.units_now, "units_after": c.units_after,
                "status": c.status, "reasons": c.reasons, "verdict": _answers(c.verdict),
                "data_source": rec.data_source}

    def record_confirmation(self, request_id: str, bank: str, blood_group: str, units: int, officer: str) -> None:
        """An officer confirmed a dispatch. This is the only effect a confirmation has."""
        self._append({"event": "CONFIRMED", "request_id": request_id, "bank": bank, "blood_group": blood_group,
                      "units": units, "officer": officer})
