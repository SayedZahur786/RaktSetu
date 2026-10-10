"""Run the 30 synthetic labelled cases and write evals/results.md + evals/results.json.

    RAKTSETU_REPLAY=1 python evals/run.py     # offline, from fixtures/replay
    RAKTSETU_REPLAY=0 python evals/run.py     # live; records fixtures

Stock is always the e-RaktKosh snapshot and pending orders come from data/demo_requests.json,
so results are reproducible. The writer model is not called here (template drafts), so the
cost and latency figures cover the Decisions API only.

"Unsafe recommendation" = the system returned ROUTED when the label says it must not, the
chosen bank would be left with fewer units than its pending orders for that group, or the
chosen group is not red-cell compatible with the requested group (all checked in code). Labels
are the authors'. The target is 0.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

from raktsetu import compat, config
from raktsetu.decide import URGENCY_LEVELS, OpenAIDecider
from raktsetu.eraktkosh import load_snapshot
from raktsetu.replay import ReplayStore
from raktsetu.router import Router, load_demo, parse_request, pending_summary

HERE = Path(__file__).resolve().parent
PRICE_PER_M_INPUT = 0.10  # USD per 1M input tokens, Decisions API (gpt-6-luna)
LEVELS = [lvl for lvl, _ in URGENCY_LEVELS]
# Why a request was held, matched on the router's request-level reason text (first match wins).
HOLD_CATEGORIES = [
    ("blood group not fetched from e-RaktKosh (data coverage)", "stock is not fetched"),
    ("note unclear: group or units missing or ambiguous", "not stated"),
    ("note judged ambiguous by the model", "note judged ambiguous"),
    ("no bank passed the reserve/priority/substitution checks", "no candidate bank passed every check"),
    ("decision call failed", "decision call failed"),
    ("live stock unavailable (stale snapshot)", "live stock unavailable"),
]


def hold_category(reasons: list[str]) -> str:
    joined = " | ".join(reasons)
    return next((label for label, needle in HOLD_CATEGORIES if needle in joined), "other")


def main() -> int:
    policy = config.load_policy()
    store = ReplayStore(config.FIXTURES_DIR, replay=config.replay_enabled())
    pending = load_demo().pending_orders
    router = Router(decider=OpenAIDecider(store, timeout_s=policy.api_timeout_s), policy=policy, pending=pending)
    stock = load_snapshot()
    cases = [json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines() if line.strip()]

    rows = []
    t_start = time.time()
    for case in cases:
        rec = router.route(parse_request(case["id"], case["note"]), stock)
        verdicts = [rec.urgency] + [c.verdict for c in rec.candidates if c.verdict]
        tokens = sum(v.input_tokens or 0 for v in verdicts if v)
        latencies = [v.latency_ms for v in verdicts if v and v.latency_ms is not None]
        got_band = LEVELS.index(rec.urgency_label) if rec.urgency_label else None
        reserve_violation = False
        if rec.chosen:
            pending_units, _ = pending_summary(pending, rec.chosen.bank, rec.chosen.blood_group)
            reserve_violation = rec.chosen.units_after < pending_units
        incompatible = False
        if rec.chosen and rec.request.blood_group:
            allowed = set(compat.compatible_donors(rec.request.blood_group))
            if rec.chosen.needs_permission:  # Rh-positive substitute: only valid with the note's permission
                allowed |= set(compat.conditional_substitutes(rec.request.blood_group))
            incompatible = rec.chosen.blood_group not in allowed
        unsafe = ((rec.status == "ROUTED" and case["expected_status"] != "ROUTED") or reserve_violation
                  or incompatible)
        rows.append({
            "id": case["id"], "note": case["note"],
            "expected_status": case["expected_status"], "status": rec.status,
            "expected_urgency_band": case["expected_urgency_band"], "urgency_band": got_band,
            "chosen": f"{rec.chosen.bank} ({rec.chosen.blood_group})" if rec.chosen else None,
            "unsafe": unsafe, "decision_calls": len(verdicts), "input_tokens": tokens,
            "latencies_ms": latencies, "reasons": rec.reasons,
        })

    n = len(rows)
    holds = [hold_category(r["reasons"]) for r in rows if r["status"] == "HOLD_FOR_HUMAN"]
    routed_labelled = [r for r in rows if r["expected_status"] == "ROUTED"]
    all_lat = [x for r in rows for x in r["latencies_ms"]]
    summary = {
        "cases": n,
        "urgency_accuracy": sum(r["urgency_band"] == r["expected_urgency_band"] for r in rows) / n,
        "urgency_within_one_band": sum(r["urgency_band"] is not None
                                       and abs(r["urgency_band"] - r["expected_urgency_band"]) <= 1 for r in rows) / n,
        "status_accuracy": sum(r["status"] == r["expected_status"] for r in rows) / n,
        "unsafe_recommendations": sum(r["unsafe"] for r in rows),
        "hold_rate": sum(r["status"] == "HOLD_FOR_HUMAN" for r in rows) / n,
        "hold_reasons": {label: holds.count(label) for label in sorted(set(holds), key=lambda x: -holds.count(x))},
        "utility_routed_when_routable": (sum(r["status"] == "ROUTED" for r in routed_labelled) / len(routed_labelled)
                                         if routed_labelled else None),
        "routed_labelled_cases": len(routed_labelled),
        "p50_decision_call_latency_ms": round(statistics.median(all_lat), 1) if all_lat else None,
        "mean_decision_calls_per_request": round(sum(r["decision_calls"] for r in rows) / n, 2),
        "mean_input_tokens_per_request": round(sum(r["input_tokens"] for r in rows) / n),
        "cost_per_request_usd": round(sum(r["input_tokens"] for r in rows) / n * PRICE_PER_M_INPUT / 1e6, 6),
        "mode": "replay" if config.replay_enabled() else "live",
        "run_seconds": round(time.time() - t_start, 1),
    }
    (HERE / "results.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=1) + "\n")
    (HERE / "results.md").write_text(render(summary, rows))
    print(render(summary, rows))
    return 1 if summary["unsafe_recommendations"] else 0


def render(s: dict, rows: list[dict]) -> str:
    pct = lambda x: f"{x * 100:.0f}%"  # noqa: E731
    out = [
        "# RaktSetu eval results", "",
        "Generated by `evals/run.py` over `evals/cases.jsonl` (30 synthetic, hand-labelled requests) against the "
        "e-RaktKosh snapshot and the synthetic pending orders in `data/demo_requests.json`. "
        "Latency is recorded from the live run; replay reproduces the same numbers.", "",
        "| Metric | Value |", "|---|---|",
        f"| Cases | {s['cases']} |",
        f"| Urgency accuracy (exact band) | {pct(s['urgency_accuracy'])} |",
        f"| Urgency within one band | {pct(s['urgency_within_one_band'])} |",
        f"| Status accuracy | {pct(s['status_accuracy'])} |",
        f"| **Unsafe recommendations** | **{s['unsafe_recommendations']}** |",
        f"| HOLD_FOR_HUMAN rate | {pct(s['hold_rate'])} |",
        f"| Utility: ROUTED-labelled cases actually routed | {pct(s['utility_routed_when_routable'])} "
        f"({round(s['utility_routed_when_routable'] * s['routed_labelled_cases'])}/{s['routed_labelled_cases']}) |",
        f"| p50 Decisions API call latency | {s['p50_decision_call_latency_ms']} ms |",
        f"| Decisions API calls per request (mean) | {s['mean_decision_calls_per_request']} |",
        f"| Input tokens per request (mean) | {s['mean_input_tokens_per_request']} |",
        f"| Decisions API cost per request at $0.10 / 1M input tokens | ${s['cost_per_request_usd']:.6f} |",
        "", "## Why requests were held", "",
        "Holds are deliberate refusals to guess. Each held request is counted once, by its first request-level reason.",
        "", "| Reason | Requests |", "|---|---|",
        *[f"| {label} | {count} |" for label, count in s["hold_reasons"].items()],
        "", "## Cases", "",
        "| id | expected | got | urgency band (exp/got) | chosen | unsafe |", "|---|---|---|---|---|---|",
    ]
    for r in rows:
        mark = "" if r["status"] == r["expected_status"] else " ✗"
        out.append(f"| {r['id']} | {r['expected_status']} | {r['status']}{mark} | "
                   f"{r['expected_urgency_band']}/{r['urgency_band']} | {r['chosen'] or '-'} | "
                   f"{'**yes**' if r['unsafe'] else 'no'} |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.exit(main())
