"""FastAPI app: GET / (landing page), GET /console (officer console), POST /route, POST /confirm, GET /source.

/confirm never dispatches anything. It appends a CONFIRMED line to the audit log, and only
for a candidate this server returned as RECOMMEND or HOLD_FOR_HUMAN. That proof travels as
an HMAC-signed confirm token, so the server keeps no per-request state and works on
stateless serverless hosts (Vercel). POST /route is rate limited per client.

    uvicorn raktsetu.api:app
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config
from .audit import AuditLog
from .models import Candidate, Recommendation, StockSnapshot
from .replay import FixtureMissing
from .router import Router, build_router, parse_request, stock_for_run

log = logging.getLogger(__name__)

LANDING = config.ROOT / "static" / "landing.html"
CONSOLE = config.ROOT / "static" / "index.html"
STOCK_TTL_S = 600
TOKEN_TTL_S = 3600
CONFIRMABLE = {"RECOMMEND", "HOLD_FOR_HUMAN"}


class RouteBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    hospital: str | None = Field(default=None, max_length=200)
    simulate_outage: bool = False


class ConfirmBody(BaseModel):
    confirm_token: str = Field(min_length=10, max_length=8000)
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


class RateLimiter:
    """Sliding-window limits per client key. In-process: on a multi-instance host each instance
    counts separately, so pair it with a spend cap on the API key."""

    def __init__(self, per_minute: int, per_day: int) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 86_400:
                hits.popleft()
            last_minute = sum(1 for t in hits if now - t <= 60)
            if last_minute >= self.per_minute or len(hits) >= self.per_day:
                return False
            hits.append(now)
            return True


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class TokenSigner:
    """Signs what a confirmation may claim: decision, bank, group, units, status and reasons."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def _sig(self, body: str) -> str:
        return hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()

    def sign(self, rec: Recommendation, c: Candidate) -> str:
        claim = {"d": rec.decision_id, "r": rec.request.id, "b": c.bank, "g": c.blood_group,
                 "u": rec.request.units, "s": c.status, "why": c.reasons, "exp": int(time.time()) + TOKEN_TTL_S}
        body = base64.urlsafe_b64encode(json.dumps(claim, separators=(",", ":")).encode()).decode()
        return f"{body}.{self._sig(body)}"

    def verify(self, token: str) -> dict[str, Any]:
        """The signed claim. Raises ValueError if forged, malformed or expired."""
        body, _, sig = token.partition(".")
        if not sig or not hmac.compare_digest(sig, self._sig(body)):
            raise ValueError("bad signature")
        claim = json.loads(base64.urlsafe_b64decode(body.encode()))
        if claim["exp"] < time.time():
            raise ValueError("expired")
        return claim


def create_app(router_factory: Callable[[bool], Router] = lambda outage: build_router(simulate_outage=outage),
               stock_loader: Callable[[], StockSnapshot] = lambda: stock_for_run(prefer_live=True),
               audit: AuditLog | None = None, limiter: RateLimiter | None = None,
               secret: bytes | None = None) -> FastAPI:
    app = FastAPI(title="RaktSetu", description="Decision support only. An officer confirms every dispatch.")
    stock = StockCache(stock_loader)
    audit_log = audit or AuditLog(config.out_dir() / "audit.jsonl")
    rate = limiter or RateLimiter(*config.rate_limits())
    signer = TokenSigner(secret or config.signing_key() or secrets.token_bytes(32))

    @app.get("/", include_in_schema=False)
    def landing() -> FileResponse:
        return FileResponse(LANDING)

    @app.get("/console", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(CONSOLE)

    @app.get("/source")
    def source() -> dict[str, str]:
        return {"data_source": stock.get().label}

    @app.post("/route")
    def route(body: RouteBody, request: Request) -> Recommendation:
        if not rate.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="Rate limit reached for this client. Try again later.")
        req = parse_request(None, body.text, body.hospital)
        try:
            rec = router_factory(body.simulate_outage).route(req, stock.get())
        except FixtureMissing as ex:
            raise HTTPException(status_code=503, detail=f"replay mode: {ex}") from ex
        if rec.request.units is not None:
            for c in rec.candidates:
                if c.status in CONFIRMABLE:
                    c.confirm_token = signer.sign(rec, c)
        return rec

    @app.post("/confirm")
    def confirm(body: ConfirmBody) -> dict[str, str]:
        try:
            claim = signer.verify(body.confirm_token)
        except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as ex:
            raise HTTPException(status_code=403, detail="invalid or expired confirm token") from ex
        if claim["s"] not in CONFIRMABLE:  # cannot happen for a token this server signed
            raise HTTPException(status_code=409, detail="only RECOMMEND or HOLD_FOR_HUMAN candidates can be confirmed")
        audit_log.record_confirmation(claim["d"], claim["r"], claim["b"], claim["g"], claim["u"], body.officer,
                                      claim["s"], claim["why"])
        log.info("officer confirmed %s (%s) -> %s", claim["d"], claim["s"], claim["b"])
        override = " Recorded as an override of HOLD_FOR_HUMAN." if claim["s"] != "RECOMMEND" else ""
        return {"status": "CONFIRMED", "note": f"Recorded in the audit log only. Nothing has been dispatched.{override}"}

    return app


app = create_app()
