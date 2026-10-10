import json

from conftest import PENDING, ScriptedDecider, bank_verdict, stock, urgency_verdict
from fastapi.testclient import TestClient

from raktsetu.api import create_app
from raktsetu.audit import AuditLog
from raktsetu.router import Router

STOCK = stock(("Bank A", "O-", 6), ("Bank B", "O-", 12))


def client(policy, tmp_path, request_verdict=None, bank=None):
    def factory(outage: bool) -> Router:
        req = ConnectionError("outage") if outage else (request_verdict or urgency_verdict())
        return Router(decider=ScriptedDecider(req, bank), policy=policy, pending=PENDING)
    audit = AuditLog(tmp_path / "audit.jsonl")
    return TestClient(create_app(factory, lambda: STOCK, audit)), tmp_path / "audit.jsonl"


def test_route_then_confirm_writes_audit_only(policy, tmp_path):
    c, audit_path = client(policy, tmp_path)
    rec = c.post("/route", json={"text": "2 units O-negative, PPH, massive transfusion"}).json()
    assert rec["status"] == "ROUTED" and rec["data_source"].startswith("snapshot")
    chosen = rec["chosen"]
    r = c.post("/confirm", json={"confirm_token": chosen["confirm_token"], "officer": "Dr. Test"})
    assert r.status_code == 200 and "Nothing has been dispatched" in r.json()["note"]
    line = json.loads(audit_path.read_text().splitlines()[-1])
    assert line["event"] == "CONFIRMED" and line["units"] == 2 and line["bank"] == chosen["bank"]


def test_blocked_candidate_cannot_be_confirmed(policy, tmp_path):
    c, _ = client(policy, tmp_path, bank=lambda t, n: bank_verdict(safe=0.0, unsafe=1.0))
    rec = c.post("/route", json={"text": "2 units O-negative, PPH"}).json()
    blocked = next(x for x in rec["candidates"] if x["status"] == "BLOCKED")
    assert blocked["confirm_token"] is None  # nothing to confirm with


def test_made_up_token_cannot_be_confirmed(policy, tmp_path):
    c, _ = client(policy, tmp_path)
    r = c.post("/confirm", json={"confirm_token": "eyJkIjoieCJ9.deadbeef", "officer": "x"})
    assert r.status_code == 403


def test_outage_holds_through_api(policy, tmp_path):
    c, _ = client(policy, tmp_path)
    rec = c.post("/route", json={"text": "2 units O-negative, PPH", "simulate_outage": True}).json()
    assert rec["status"] == "HOLD_FOR_HUMAN" and rec["chosen"] is None


def test_empty_text_rejected(policy, tmp_path):
    c, _ = client(policy, tmp_path)
    assert c.post("/route", json={"text": ""}).status_code == 422


def test_landing_and_console_served(policy, tmp_path):
    c, _ = client(policy, tmp_path)
    landing = c.get("/")
    assert landing.status_code == 200 and 'href="/console"' in landing.text
    console = c.get("/console")
    assert console.status_code == 200 and "Decision support only" in console.text
