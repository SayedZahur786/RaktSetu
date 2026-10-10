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

## Rules

- Thresholds live only in `policy.yaml`. Never hardcode one in Python.
- `decide.py` is the only file that touches the Decisions API request/response format.
- The model judges; code decides. Arithmetic and compatibility stay in Python.
- Fail closed: any error, refusal or missing answer must end in `HOLD_FOR_HUMAN`.
- Never dispatch. `/confirm` writes to the audit log and nothing else.
- Never commit `.env` or print `OPENAI_API_KEY`.
