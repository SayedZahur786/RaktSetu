"""FastAPI officer console: POST /route, POST /confirm, GET / (static/index.html).

/confirm never dispatches anything. It appends a CONFIRMED line to the audit log, and only
for a bank that appeared as RECOMMEND or HOLD_FOR_HUMAN in a recommendation this server made.

    uvicorn raktsetu.api:app
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config
from .audit import AuditLog
from .models import Recommendation, StockSnapshot
from .replay import FixtureMissing
from .router import Router, build_router, parse_request, stock_for_run

log = logging.getLogger(__name__)

INDEX = config.ROOT / "static" / "index.html"
STOCK_TTL_S = 600
MAX_REMEMBERED = 200
CONFIRMABLE = {"RECOMMEND", "HOLD_FOR_HUMAN"}


class RouteBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    hospital: str | None = Field(default=None, max_length=200)
    simulate_outage: bool = False


class ConfirmBody(BaseModel):
    decision_id: str
    bank: str
    blood_group: str
    officer: str = Field(min_length=1, max_length=100)


class StockCache:
    def __init__(self, loader: Callable[[], StockSnapshot]) -> None:
        self._loader = loader
        self._lock = threading.Lock()
        self._snap: StockSnapshot | None = None
        self._at = 0.0

    def get(self) -> StockSnapshot:
        with self._lock:
            if self._snap is None or time.time() - self._at > STOCK_TTL_S:
                self._snap, self._at = self._loader(), time.time()
            return self._snap


def create_app(router_factory: Callable[[bool], Router] = lambda outage: build_router(simulate_outage=outage),
               stock_loader: Callable[[], StockSnapshot] = lambda: stock_for_run(prefer_live=True),
               audit: AuditLog | None = None) -> FastAPI:
    app = FastAPI(title="RaktSetu", description="Decision support only. An officer confirms every dispatch.")
    stock = StockCache(stock_loader)
    audit_log = audit or AuditLog(config.OUT_DIR / "audit.jsonl")
    recent: OrderedDict[str, Recommendation] = OrderedDict()
    recent_lock = threading.Lock()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(INDEX)

    @app.get("/source")
    def source() -> dict[str, str]:
        return {"data_source": stock.get().label}

    @app.post("/route")
    def route(body: RouteBody) -> Recommendation:
        req = parse_request(None, body.text, body.hospital)
        try:
            rec = router_factory(body.simulate_outage).route(req, stock.get())
        except FixtureMissing as ex:
            raise HTTPException(status_code=503, detail=f"replay mode: {ex}") from ex
        with recent_lock:
            recent[rec.decision_id] = rec
            while len(recent) > MAX_REMEMBERED:
                recent.popitem(last=False)
        return rec

    @app.post("/confirm")
    def confirm(body: ConfirmBody) -> dict[str, str]:
        with recent_lock:
            rec = recent.get(body.decision_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown decision_id")
        cand = next((c for c in rec.candidates if c.bank == body.bank and c.blood_group == body.blood_group), None)
        if cand is None or cand.status not in CONFIRMABLE:
            raise HTTPException(status_code=409, detail="only RECOMMEND or HOLD_FOR_HUMAN candidates can be confirmed")
        if rec.request.units is None:  # unreachable: candidates only exist when units were parsed
            raise HTTPException(status_code=409, detail="request has no unit count")
        audit_log.record_confirmation(rec.decision_id, rec.request.id, cand.bank, cand.blood_group, rec.request.units,
                                      body.officer, cand.status, cand.reasons)
        log.info("officer confirmed %s (%s) -> %s", rec.decision_id, cand.status, cand.bank)
        override = " Recorded as an override of HOLD_FOR_HUMAN." if cand.status != "RECOMMEND" else ""
        return {"status": "CONFIRMED", "note": f"Recorded in the audit log only. Nothing has been dispatched.{override}"}

    return app


app = create_app()
