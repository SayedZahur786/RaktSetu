"""Measure RaktSetu end to end against LIVE e-RaktKosh stock and write evals/latency.json.

    RAKTSETU_REPLAY=0 python evals/latency.py [N]  # first N cases (default all); needs OPENAI_API_KEY

Unlike evals/run.py (which replays recorded fixtures), this times the real path: one live
e-RaktKosh pull, then every case in evals/cases.jsonl routed through the Decisions API and the
GPT-6.1 Sol writer. Model calls are recorded to a throwaway directory, so the committed
fixtures/replay/ set is never changed. Stock fetch time is reported separately because a
deployment fetches stock on a timer (api.py caches it), not once per request.
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from raktsetu import config
from raktsetu.decide import OpenAIDecider
from raktsetu.eraktkosh import fetch_live
from raktsetu.replay import ReplayStore
from raktsetu.router import Router, load_demo, parse_request
from raktsetu.writer import SolWriter

HERE = Path(__file__).resolve().parent


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def main() -> int:
    if config.replay_enabled() or not config.openai_api_key():
        print("latency.py measures live calls: set RAKTSETU_REPLAY=0 and OPENAI_API_KEY", file=sys.stderr)
        return 2
    policy = config.load_policy()
    state, district = config.region_codes()

    t0 = time.perf_counter()
    stock = fetch_live(state, district)  # raises if e-RaktKosh is down: a measurement must be live
    fetch_s = time.perf_counter() - t0
    banks = sorted({b.name for b in stock.banks})

    with tempfile.TemporaryDirectory() as tmp:
        store = ReplayStore(Path(tmp), replay=False)
        router = Router(decider=OpenAIDecider(store, timeout_s=policy.api_timeout_s), policy=policy,
                        pending=load_demo().pending_orders, writer=SolWriter(store, timeout_s=policy.api_timeout_s))
        cases = [json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines() if line.strip()]
        cases = cases[: int(sys.argv[1])] if len(sys.argv) > 1 else cases
        timings, statuses, fallbacks = [], [], 0
        for case in cases:
            t1 = time.perf_counter()
            rec = router.route(parse_request(case["id"], case["note"]), stock)
            timings.append(time.perf_counter() - t1)
            statuses.append(rec.status)
            fallbacks += int(bool(rec.draft and rec.draft.fallback))

    result = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stock": {"source": stock.source, "region": stock.region, "fetched_at": stock.fetched_at,
                  "state_code": state, "district_code": district, "banks": len(banks),
                  "bank_group_records": len(stock.banks), "groups": stock.groups,
                  "fetch_seconds": round(fetch_s, 2)},
        "requests": len(timings),
        "route_seconds": {"p50": round(statistics.median(timings), 2), "p90": round(pct(timings, 0.9), 2),
                          "max": round(max(timings), 2)},
        "includes": "Decisions API calls (request + every candidate bank) and the GPT-6.1 Sol writer; "
                    "excludes the stock fetch, reported separately",
        "writer_template_fallbacks": fallbacks,
        "statuses": {s: statuses.count(s) for s in sorted(set(statuses))},
    }
    (HERE / "latency.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
