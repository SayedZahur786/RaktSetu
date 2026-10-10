# Architecture

RaktSetu turns a hospital's free-text blood request into a ranked, explained recommendation. A blood bank officer then confirms or rejects it. The models judge and write; Python decides. Every number below comes from a committed file.

**Statuses.** Request level: `ROUTED`, `HOLD_FOR_HUMAN`, `NO_STOCK`. Candidate level: `RECOMMEND`, `HOLD_FOR_HUMAN`, `BLOCKED`, `NO_STOCK`.

## Request flow

```
hospital note ──► parse_request (code)          group + units; ambiguous → None → HOLD_FOR_HUMAN
                     │
e-RaktKosh ──────► fetch_live / load_stock       live, or snapshot (fallback flagged → no RECOMMEND)
                     │
                     ├─► Decisions API #1         urgency (score), note_ambiguous (predicate)
                     │      └─ request_gate (code, policy.yaml)
                     │
                     ├─► candidates (code)         every bank with stock: units now/after, pending units
                     │
                     ├─► Decisions API, per bank   reserve_check, priority_check (choice), concurrent
                     │   Decisions API, once       substitution_ok (predicate), only for Rh-positive substitutes
                     │      └─ candidate_status (code, policy.yaml) → RECOMMEND / HOLD_FOR_HUMAN / BLOCKED / NO_STOCK
                     │
                     ├─► rank (code)               exact > always-compatible > note-permitted; most units left
                     ├─► GPT-6.1 Sol               bank request, officer explanation, follow-up question
                     └─► audit log                 out/audit.jsonl, every candidate with raw probabilities
                             │
officer ──────────► POST /confirm                 audit only; nothing is dispatched
```

Measured on the live path (10 October 2026, n=5, [evals/latency.json](evals/latency.json)):

- the stock pull took 0.88 s for 16 banks;
- a full request, Decisions API plus Sol, took 6.3 s p50.

Over the 30-case eval ([evals/results.md](evals/results.md)), a request makes 5.2 Decisions API calls on average, and each call takes 166 ms p50.

## Modules and what each owns

| Module | Owns | Knows a wire format? |
|---|---|---|
| [eraktkosh.py](src/raktsetu/eraktkosh.py) | Live e-RaktKosh client (`fetch_live`), snapshot fallback (`load_stock`) | e-RaktKosh |
| [decide.py](src/raktsetu/decide.py) | The questions; `OpenAIDecider`; `parse_decision` with range checks | Decisions API |
| [writer.py](src/raktsetu/writer.py) | `SolWriter` (Responses API, JSON schema output, effort `low`), template fallback | Responses API |
| [compat.py](src/raktsetu/compat.py) | ABO/Rh red-cell compatibility, blood-group parsing (pure) | none |
| [policy.py](src/raktsetu/policy.py) | Thresholds from [policy.yaml](policy.yaml) applied to verdicts; ranking (pure) | none |
| [router.py](src/raktsetu/router.py) | Orchestration and the fail-closed wrapper (`Router._ask`) | none |
| [replay.py](src/raktsetu/replay.py) | Record/replay of model calls keyed by request sha256 | none |
| [audit.py](src/raktsetu/audit.py) | Append-only JSONL audit | none |
| [api.py](src/raktsetu/api.py) | FastAPI: `POST /route`, `POST /confirm`, `GET /source`, `GET /` | none |
| [models.py](src/raktsetu/models.py), [config.py](src/raktsetu/config.py) | Pydantic boundary models; env and policy loading | none |

Swapping the decision model means implementing the `Decider` protocol in `decide.py`; nothing else changes.

## Where it fails closed

Every one of these ends in `HOLD_FOR_HUMAN`, never `RECOMMEND`:

| Condition | Where |
|---|---|
| API error, timeout or auth failure; error text scrubbed of key-like strings | `router.py` `Router._ask` |
| Refusal, missing answer, wrong answer type | `policy.py` `_answer` |
| NaN, infinite or out-of-range probability | `decide.py` `_num`; `models.py` `Prob` |
| Ambiguous note, or low urgency confidence | `policy.py` `request_gate` |
| Group or units missing or ambiguous in the note | `router.py` `parse_request`, `Router.route` |
| Group not fetched from e-RaktKosh | `router.py` `Router.route` |
| Live fetch failed, so the snapshot was used | `eraktkosh.py` `load_stock`; `router.py` `Router.route` |
| Dispatch would leave less than pending orders need, or empty the bank | `policy.py` `candidate_status` |
| Substitution permission uncertain or failed | `policy.py` `candidate_status`; `router.py` `Router._judge` |

Tests: [tests/test_router_failclosed.py](tests/test_router_failclosed.py), [tests/test_hardening.py](tests/test_hardening.py), [tests/test_policy.py](tests/test_policy.py).

## Replay and reproducibility

- **Live mode** (`RAKTSETU_REPLAY=0`) records every model request/response to `fixtures/replay/<sha256>.json`.
- **Replay mode** (`RAKTSETU_REPLAY=1`) serves those files back with no network calls and no key, and raises `FixtureMissing` for anything unrecorded.
- **Replay always uses the snapshot**, because live stock changes daily and would change the request hashes.
- **CI** ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the tests, the demo and the eval in replay mode.
- [evals/latency.py](evals/latency.py) is the one tool that measures live; it records to a throwaway directory.

## Not in scope yet

- Placing orders in a bank's own system.
- Transport.
- Authentication.
- Blood groups other than O-negative and O-positive.
- Real pending-order data (the pending orders are synthetic).
