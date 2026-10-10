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


def openai_api_key() -> str | None:
    """The key, or None. Never log or print the return value."""
    return os.environ.get("OPENAI_API_KEY") or None


def region_codes() -> tuple[str, str]:
    state = os.environ.get("ERAKTKOSH_STATE", "").strip() or DEFAULT_STATE_CODE
    district = os.environ.get("ERAKTKOSH_DISTRICT", "").strip() or DEFAULT_DISTRICT_CODE
    return state, district
