"""Behaviour needed to run as a stateless, read-only serverless function (Vercel)."""
import json

from conftest import PENDING, ScriptedDecider, stock, urgency_verdict
from fastapi.testclient import TestClient

from raktsetu import api as api_mod
from raktsetu import config
from raktsetu.api import RateLimiter, create_app
from raktsetu.audit import AuditLog
from raktsetu.replay import ReplayStore
from raktsetu.router import Router

STOCK = stock(("Bank A", "O-", 6), ("Bank B", "O-", 12))
NOTE = "2 units O-negative, PPH, massive transfusion"


def make_client(policy, tmp_path, limiter=None, secret=b"test-secret"):
    def factory(outage: bool) -> Router:
        return Router(decider=ScriptedDecider(urgency_verdict()), policy=policy, pending=PENDING)
    app = create_app(factory, lambda: STOCK, AuditLog(tmp_path / "audit.jsonl"),
                     limiter or RateLimiter(per_minute=100, per_day=1000), secret)
    return TestClient(app)


def test_live_without_recording_writes_nothing(tmp_path):
    store = ReplayStore(tmp_path / "fx", replay=False, record=False)
    resp, _ = store.call({"api": "x"}, lambda: {"ok": True})
    assert resp == {"ok": True} and not (tmp_path / "fx").exists()


def test_record_flag_from_env(monkeypatch):
    monkeypatch.setenv("RAKTSETU_RECORD", "0")
    assert config.record_enabled() is False
    monkeypatch.delenv("RAKTSETU_RECORD")
    assert config.record_enabled() is True


def test_out_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RAKTSETU_OUT_DIR", str(tmp_path))
    assert config.out_dir() == tmp_path
    monkeypatch.delenv("RAKTSETU_OUT_DIR")
    assert config.out_dir() == config.OUT_DIR


def test_confirm_works_on_a_fresh_instance_via_signed_token(policy, tmp_path):
    """No server memory: a different app instance with the same secret accepts the token."""
    rec = make_client(policy, tmp_path).post("/route", json={"text": NOTE}).json()
    token = rec["chosen"]["confirm_token"]
    other = make_client(policy, tmp_path)
    r = other.post("/confirm", json={"confirm_token": token, "officer": "Dr. Test"})
    assert r.status_code == 200
    line = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert line["event"] == "CONFIRMED" and line["bank"] == rec["chosen"]["bank"] and line["units"] == 2
    assert line["decision_id"] == rec["decision_id"] and line["override"] is False


def test_tampered_or_foreign_token_is_rejected(policy, tmp_path):
    rec = make_client(policy, tmp_path).post("/route", json={"text": NOTE}).json()
    token = rec["chosen"]["confirm_token"]
    body, sig = token.split(".")
    forged = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig
    c = make_client(policy, tmp_path)
    assert c.post("/confirm", json={"confirm_token": forged, "officer": "x"}).status_code == 403
    assert make_client(policy, tmp_path, secret=b"other").post(
        "/confirm", json={"confirm_token": token, "officer": "x"}).status_code == 403


def test_expired_token_is_rejected(policy, tmp_path, monkeypatch):
    rec = make_client(policy, tmp_path).post("/route", json={"text": NOTE}).json()
    token = rec["chosen"]["confirm_token"]
    monkeypatch.setattr(api_mod.time, "time", lambda: 10**12)
    assert make_client(policy, tmp_path).post(
        "/confirm", json={"confirm_token": token, "officer": "x"}).status_code == 403


def test_blocked_candidates_get_no_token(policy, tmp_path):
    rec = make_client(policy, tmp_path).post("/route", json={"text": NOTE}).json()
    for c in rec["candidates"]:
        assert (c["confirm_token"] is not None) == (c["status"] in ("RECOMMEND", "HOLD_FOR_HUMAN"))


def test_rate_limit_per_client(policy, tmp_path):
    c = make_client(policy, tmp_path, limiter=RateLimiter(per_minute=2, per_day=100))
    codes = [c.post("/route", json={"text": NOTE}, headers={"x-forwarded-for": "1.2.3.4"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]
    assert c.post("/route", json={"text": NOTE}, headers={"x-forwarded-for": "5.6.7.8"}).status_code == 200


def test_daily_limit():
    limiter = RateLimiter(per_minute=100, per_day=2)
    assert [limiter.allow("ip", now=t) for t in (0, 100, 200)] == [True, True, False]
    assert limiter.allow("ip", now=86_401 + 100)
