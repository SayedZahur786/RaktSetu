"""Command line: `python -m raktsetu route ...` and `python -m raktsetu demo`."""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .models import Candidate, Recommendation
from .replay import FixtureMissing
from .router import build_router, load_demo, parse_request, stock_for_run

BANNER = "RaktSetu · synthetic requests · decision support only · an officer confirms every dispatch"


def _verdict_cell(c: Candidate) -> str:
    v = c.verdict
    if v is None:
        return "-"
    if v.error:
        return f"error: {v.error}"
    parts = []
    for name, a in v.answers.items():
        short = {"reserve_check": "reserve", "priority_check": "priority", "substitution_ok": "subst"}.get(name, name)
        if a.kind == "choice":
            parts.append(f"{short}={a.choice}({a.probabilities.get(a.choice or '', 0):.2f})")
        elif a.kind == "predicate":
            parts.append(f"{short}=p{a.probability:.2f}")
        else:
            parts.append(f"{short}={a.kind}")
    return " ".join(parts)


def print_recommendation(rec: Recommendation, title: str | None = None) -> None:
    head = f"{rec.request.id}" + (f" · {title}" if title else "")
    print(f"\n{'━' * 100}\n{head}\n{'━' * 100}")
    print(f"note: {rec.request.note}")
    print(f"parsed: group={rec.request.blood_group or '?'} units={rec.request.units or '?'} · "
          f"urgency={rec.urgency_label or 'unknown'}")
    print(f"STATUS: {rec.status}" + (f"  ({'; '.join(rec.reasons)})" if rec.reasons else ""))
    shown = [c for c in rec.candidates if c.status != "NO_STOCK"]
    no_stock = len(rec.candidates) - len(shown)
    if shown:
        print(f"\n  {'status':<15} {'bank':<52} {'grp':<4} {'now→after':<10} verdicts")
        for c in shown:
            grp = c.blood_group + ("" if c.exact else "*")
            print(f"  {c.status:<15} {c.bank[:52]:<52} {grp:<4} {f'{c.units_now}→{c.units_after}':<10} {_verdict_cell(c)}")
            if c.status != "RECOMMEND":
                print(f"  {'':<15} ↳ {'; '.join(c.reasons)}")
    if no_stock:
        print(f"  ({no_stock} bank(s) with some but not enough stock: NO_STOCK)")
    if any(not c.exact for c in shown):
        print("  * substitute group")
    if rec.draft:
        tag = "template fallback" if rec.draft.fallback else rec.draft.model
        print(f"\n  officer explanation [{tag}]: {rec.draft.officer_explanation}")
        print(f"  bank request draft: {rec.draft.bank_request}")
        if rec.draft.follow_up_question:
            print(f"  follow-up question: {rec.draft.follow_up_question}")
    if rec.status == "ROUTED":
        print("\n  Nothing is dispatched until an officer confirms (POST /confirm writes CONFIRMED to the audit log).")


def cmd_route(args: argparse.Namespace) -> int:
    stock = stock_for_run(prefer_live=not args.snapshot)
    print(f"{BANNER}\ndata: {stock.label}")
    if args.request_id:
        demo = load_demo()
        sc = next((s for s in demo.scenarios if s.id == args.request_id), None)
        if sc is None:
            print(f"unknown request id {args.request_id!r}; known: {[s.id for s in demo.scenarios]}", file=sys.stderr)
            return 2
        req, outage = parse_request(sc.id, sc.note, sc.hospital), args.simulate_outage or sc.simulate_outage
    else:
        req, outage = parse_request(f"req-{int(time.time())}", args.text, args.hospital), args.simulate_outage
    rec = build_router(simulate_outage=outage).route(req, stock)
    print_recommendation(rec)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    stock = stock_for_run(prefer_live=args.live_stock)
    print(f"{BANNER}\ndata: {stock.label}")
    demo = load_demo()
    rows = []
    for sc in demo.scenarios:
        router = build_router(simulate_outage=args.simulate_outage or sc.simulate_outage)
        rec = router.route(parse_request(sc.id, sc.note, sc.hospital), stock)
        print_recommendation(rec, sc.title)
        expected = "HOLD_FOR_HUMAN" if args.simulate_outage else sc.expected_status  # outage must always hold
        rows.append((sc.id, sc.title, rec.status, expected, rec.chosen.bank if rec.chosen else "-"))
    print(f"\n{'━' * 100}\nSUMMARY")
    for rid, title, got, want, bank in rows:
        mark = "ok " if got == want else "!! "
        print(f"  {mark}{rid:<6} {title:<46} {got:<15} (expected {want:<14}) {bank[:40]}")
    print("Audit log: out/audit.jsonl")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="raktsetu", description=BANNER)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("route", help="route one request")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="free-text request note")
    src.add_argument("--request-id", help="a scenario id from data/demo_requests.json")
    r.add_argument("--hospital", default=None)
    r.add_argument("--snapshot", action="store_true", help="use the cached e-RaktKosh snapshot")
    r.add_argument("--simulate-outage", action="store_true", help="make every Decisions API call fail")
    r.set_defaults(fn=cmd_route)
    d = sub.add_parser("demo", help="run the four demo scenarios")
    d.add_argument("--simulate-outage", action="store_true", help="make every Decisions API call fail")
    d.add_argument("--live-stock", action="store_true", help="use live e-RaktKosh stock instead of the snapshot")
    d.set_defaults(fn=cmd_demo)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except FixtureMissing as ex:
        print(f"replay error: {ex}", file=sys.stderr)
        return 3
