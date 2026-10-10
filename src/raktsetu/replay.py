"""Record/replay of external model calls, keyed by the sha256 of the request payload.

Live mode (RAKTSETU_REPLAY=0) records every request/response pair to fixtures/replay/.
RAKTSETU_RECORD=0 turns recording off for read-only hosts.
Replay mode (RAKTSETU_REPLAY=1) serves them back with no network, and raises
FixtureMissing when a request was never recorded. Payloads never contain credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class FixtureMissing(RuntimeError):
    """Replay mode asked for a request that was never recorded."""


def payload_key(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReplayStore:
    def __init__(self, directory: Path, replay: bool, record: bool = True) -> None:
        self.directory = directory
        self.replay = replay
        self.record = record  # live mode only: False on read-only hosts

    def call(self, payload: dict[str, Any], live: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], float]:
        """Return (response, latency_ms): recorded in replay mode, else from `live()` and recorded."""
        key = payload_key(payload)
        path = self.directory / f"{key}.json"
        if self.replay:
            if not path.exists():
                raise FixtureMissing(
                    f"No recorded response for {payload.get('api')} request {key[:12]}… in {self.directory}. "
                    "Run once live (RAKTSETU_REPLAY=0 with OPENAI_API_KEY set) to record it.")
            record = json.loads(path.read_text())
            return record["response"], float(record["latency_ms"])
        t0 = time.perf_counter()
        response = live()
        latency_ms = (time.perf_counter() - t0) * 1000
        if not self.record:
            return response, latency_ms
        self._write(path, {"request": payload, "response": response, "latency_ms": round(latency_ms, 1),
                           "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return response, latency_ms

    def _write(self, path: Path, record: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=1, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
