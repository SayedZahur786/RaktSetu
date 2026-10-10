# AGENTS.md

## Setup

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt          # installs the package in editable mode too

## Test (no key, no network)

    RAKTSETU_REPLAY=1 pytest -q

## Run

    RAKTSETU_REPLAY=1 python -m raktsetu demo                    # 4 scenarios from recorded fixtures
    RAKTSETU_REPLAY=1 python -m raktsetu demo --simulate-outage  # every decision call fails -> HOLD
    RAKTSETU_REPLAY=1 python evals/run.py                        # rewrites evals/results.md
    RAKTSETU_REPLAY=0 python evals/latency.py 5                  # live timing -> evals/latency.json (key needed)
    RAKTSETU_REPLAY=1 uvicorn raktsetu.api:app                   # officer console
    python -m raktsetu route --text "..."                        # live: needs OPENAI_API_KEY in .env

Live runs (`RAKTSETU_REPLAY=0`) record every model call to `fixtures/replay/<sha256>.json`.
Commit new fixtures whenever questions, prompts or demo/eval inputs change; otherwise replay
raises `FixtureMissing`.

## Module map (`src/raktsetu/`)

- `config.py`: env vars, `.env`, `policy.yaml` loading, model ids
- `models.py`: pydantic models shared across modules (Request, BankStock, Verdict, Candidate, Recommendation, Policy)
- `eraktkosh.py`: e-RaktKosh live client plus snapshot fallback (only file that knows that format)
- `compat.py`: ABO/Rh compatibility table and blood-group parsing (pure)
- `decide.py`: questions plus the Decisions API adapter (only file that knows that format)
- `replay.py`: record/replay of model calls keyed by request hash
- `policy.py`: verdicts plus thresholds give statuses, and ranking (pure)
- `router.py`: orchestration: request, candidates, decisions, policy, rank, draft, audit
- `writer.py`: GPT-6.1 Sol via the Responses API, with a template fallback (only file that knows that format)
- `audit.py`: append-only `out/audit.jsonl`
- `cli.py`, `__main__.py`: `python -m raktsetu route|demo`
- `api.py`: FastAPI, `POST /route`, `POST /confirm` (audit only), `GET /`

## Claim -> evidence

Every claim in README.md and where it is proved. Update this table whenever a claim changes.

| README claim | Evidence |
|---|---|
| Reads the live e-RaktKosh API | `src/raktsetu/eraktkosh.py` `fetch_live`; live pull result in `evals/latency.json` (`stock.source = live`) |
| 16 banks pulled in 0.88 s; 6.3 s p50 per request (n=5, live) | `evals/latency.json`, produced by `evals/latency.py` |
| 166 ms p50 per Decisions API call; 5.2 calls and $0.000236 per request | `evals/results.md` / `evals/results.json`, produced by `evals/run.py` |
| 0 unsafe recommendations, 97% status accuracy over 30 cases | `evals/results.md`; cases in `evals/cases.jsonl` |
| Fails closed (errors, refusals, malformed numbers, stale stock) | `tests/test_hardening.py`, `tests/test_router_failclosed.py`, `tests/test_policy.py`; `src/raktsetu/policy.py`, `src/raktsetu/router.py` `Router._ask` |
| The model judges, code decides; thresholds only in policy.yaml | `policy.yaml`; `src/raktsetu/policy.py` `request_gate`, `candidate_status` |
| Pending-order shortfall and "never empty a bank" are enforced in code | `src/raktsetu/policy.py` `candidate_status`; `tests/test_hardening.py::test_policy_holds_when_dispatch_leaves_less_than_pending`, `tests/test_policy.py::test_dispatch_that_empties_bank_holds` |
| Substitution asked once per request | `src/raktsetu/router.py` `Router._judge`; `tests/test_hardening.py::test_substitution_asked_once_and_applied_to_every_conditional_bank` |
| ABO/Rh compatibility is correct | `src/raktsetu/compat.py`; `tests/test_compat.py` |
| Decisions API typed questions and parsing | `src/raktsetu/decide.py` `OpenAIDecider.ask`, `parse_decision`; `tests/test_decide_parse.py` |
| GPT-6.1 Sol writes text and cannot change the decision | `src/raktsetu/writer.py` `INSTRUCTIONS`, `SolWriter.write`; `tests/test_decide_parse.py::test_writer_parses_sol_json` |
| Nothing is dispatched; confirm only writes the audit log, overrides flagged | `src/raktsetu/api.py` `confirm`; `src/raktsetu/audit.py` `record_confirmation`; `tests/test_api.py` |
| Any covered district works with no code change | `src/raktsetu/config.py` `region_codes`; `.env.example` |
| Runs offline with no key | `src/raktsetu/replay.py` `ReplayStore`; `fixtures/replay/`; `.github/workflows/ci.yml` |
| Before-figures (Bihar 1–6 h, Kerala ~31 min, 2,903 banks, 15% short, south Asia shortfall) | Source links inline in README.md "The problem" |

## Rules

- Thresholds live only in `policy.yaml`. Never hardcode one in Python.
- `decide.py` is the only file that touches the Decisions API request/response format.
- The model judges; code decides. Arithmetic and compatibility stay in Python.
- Fail closed: any error, refusal or missing answer must end in `HOLD_FOR_HUMAN`.
- Never dispatch. `/confirm` writes to the audit log and nothing else.
- Never commit `.env` or print `OPENAI_API_KEY`.
