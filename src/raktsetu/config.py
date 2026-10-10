"""Environment and policy.yaml loading. The only place env vars and policy values are read."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import Policy

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
FIXTURES_DIR = ROOT / "fixtures" / "replay"
OUT_DIR = ROOT / "out"
POLICY_FILE = ROOT / "policy.yaml"

DECISION_MODEL = "gpt-6-luna"
WRITER_MODEL = "gpt-6.1-sol"
WRITER_REASONING_EFFORT = "low"

# e-RaktKosh defaults match data/eraktkosh_snapshot.json (Andhra Pradesh / East Godavari).
DEFAULT_STATE_CODE = "28"
DEFAULT_DISTRICT_CODE = "545"

load_dotenv(ROOT / ".env", override=False)


def load_policy(path: Path = POLICY_FILE) -> Policy:
    policy = Policy.model_validate(yaml.safe_load(path.read_text()))
    if policy.fail_mode != "closed":
        raise ValueError(f"policy.yaml fail_mode must be 'closed', got {policy.fail_mode!r}")
    return policy


def replay_enabled() -> bool:
    return os.environ.get("RAKTSETU_REPLAY", "0").strip() == "1"


def record_enabled() -> bool:
    """Live calls are recorded to fixtures/ unless RAKTSETU_RECORD=0 (read-only hosts such as Vercel)."""
    return os.environ.get("RAKTSETU_RECORD", "1").strip() != "0"


def out_dir() -> Path:
    """Where the audit log goes. Serverless hosts point this at a writable path, e.g. /tmp/raktsetu."""
    override = os.environ.get("RAKTSETU_OUT_DIR", "").strip()
    return Path(override) if override else OUT_DIR


def rate_limits() -> tuple[int, int]:
    """(requests per minute, requests per day) per client on POST /route."""
    return (int(os.environ.get("RAKTSETU_RATE_PER_MINUTE", "5")), int(os.environ.get("RAKTSETU_RATE_PER_DAY", "50")))


def signing_key() -> bytes | None:
    """HMAC key for confirm tokens. Unset means a random per-process key (fine for one process)."""
    key = os.environ.get("RAKTSETU_SIGNING_KEY", "").strip()
    return key.encode() if key else None


def openai_api_key() -> str | None:
    """The key, or None. Never log or print the return value."""
    return os.environ.get("OPENAI_API_KEY") or None


def region_codes() -> tuple[str, str]:
    state = os.environ.get("ERAKTKOSH_STATE", "").strip() or DEFAULT_STATE_CODE
    district = os.environ.get("ERAKTKOSH_DISTRICT", "").strip() or DEFAULT_DISTRICT_CODE
    return state, district
