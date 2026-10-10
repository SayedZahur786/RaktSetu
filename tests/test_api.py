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
    r = c.post("/confirm", json={"decision_id": rec["decision_id"], "bank": chosen["bank"],
                                 "blood_group": chosen["blood_group"], "officer": "Dr. Test"})
    assert r.status_code == 200 and "Nothing has been dispatched" in r.json()["note"]
    line = json.loads(audit_path.read_text().splitlines()[-1])
    assert line["event"] == "CONFIRMED" and line["units"] == 2 and line["bank"] == chosen["bank"]


def test_blocked_candidate_cannot_be_confirmed(policy, tmp_path):
    c, _ = client(policy, tmp_path, bank=lambda t, n: bank_verdict(safe=0.0, unsafe=1.0))
    rec = c.post("/route", json={"text": "2 units O-negative, PPH"}).json()
    blocked = next(x for x in rec["candidates"] if x["status"] == "BLOCKED")
    r = c.post("/confirm", json={"decision_id": rec["decision_id"], "bank": blocked["bank"],
                                 "blood_group": blocked["blood_group"], "officer": "Dr. Test"})
    assert r.status_code == 409


def test_unknown_request_cannot_be_confirmed(policy, tmp_path):
    c, _ = client(policy, tmp_path)
    r = c.post("/confirm", json={"decision_id": "nope", "bank": "Bank B", "blood_group": "O-", "officer": "x"})
    assert r.status_code == 404


def test_outage_holds_through_api(policy, tmp_path):
    c, _ = client(policy, tmp_path)
    rec = c.post("/route", json={"text": "2 units O-negative, PPH", "simulate_outage": True}).json()
    assert rec["status"] == "HOLD_FOR_HUMAN" and rec["chosen"] is None


def test_empty_text_rejected(policy, tmp_path):
    c, _ = client(policy, tmp_path)
    assert c.post("/route", json={"text": ""}).status_code == 422


def test_index_served(policy, tmp_path):
    c, _ = client(policy, tmp_path)
    r = c.get("/")
    assert r.status_code == 200 and "Decision support only" in r.text
