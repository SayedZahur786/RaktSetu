"""e-RaktKosh stock: live client (ported from ekrakt's agent.py) plus the cached snapshot.

The only file that knows the e-RaktKosh wire format. Live fetch falls back to the snapshot
on any failure or an empty district, and the returned StockSnapshot says which was used.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests

from .compat import from_eraktkosh
from .config import DATA_DIR, DEFAULT_DISTRICT_CODE, DEFAULT_STATE_CODE
from .models import BankStock, BloodGroup, StockSnapshot

log = logging.getLogger(__name__)

URL = "https://eraktkosh.mohfw.gov.in/eraktkoshPortal/eraktkosh/blood-availability"
COMPONENT = {"name": "Packed Red Blood Cells", "code": "12"}
# e-RaktKosh bloodGroupId values verified against the live API. Only these are fetched,
# which is also all the snapshot holds.
GROUP_IDS: dict[BloodGroup, str] = {"O-": "16", "O+": "15"}
SNAPSHOT_FILE = DATA_DIR / "eraktkosh_snapshot.json"
SNAPSHOT_REGION = "East Godavari, Andhra Pradesh"
TIMEOUT_S = 15


def parse_qty(label: str | None) -> int:
    """'O-Ve : 12' -> 12. Negative counts (seen in real data) are treated as 0."""
    m = re.search(r":\s*(-?\d+)", label or "")
    return max(0, int(m.group(1))) if m else 0


def _bank_from_label(name: str, category: str | None, label: str, updated: str | None) -> BankStock | None:
    group = from_eraktkosh(label.split(":")[0])
    if group is None or not name:
        return None
    return BankStock(name=name, category=category, blood_group=group, units=parse_qty(label), last_updated=updated)


def load_snapshot(path: Path = SNAPSHOT_FILE) -> StockSnapshot:
    snap = json.loads(path.read_text())
    banks = [b for raw in snap["banks"]
             if (b := _bank_from_label(raw["name"], raw.get("category"), raw["stock"], raw.get("last_updated")))]
    return StockSnapshot(banks=banks, source="snapshot", fetched_at=snap["fetched_at"], region=SNAPSHOT_REGION,
                         groups=sorted({b.blood_group for b in banks}))


def fetch_live(state_code: str, district_code: str, session: requests.Session | None = None) -> StockSnapshot:
    """Fetch packed red cell stock for every group in GROUP_IDS. Raises on HTTP or format errors."""
    http = session or requests.Session()
    banks: list[BankStock] = []
    region = ""
    for group, gid in GROUP_IDS.items():
        r = http.get(URL, timeout=TIMEOUT_S, params={
            "stateCode": state_code, "districtId": district_code,
            "componentId": COMPONENT["code"], "bloodGroupId": gid})
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise ValueError(f"unexpected e-RaktKosh payload type {type(rows).__name__}")
        for e in rows:
            info = (e.get("components") or {}).get(COMPONENT["name"], {})
            label = info.get("available_WithQty") or info.get("not_available_WithQty") or f"{group}Ve : 0"
            bank = _bank_from_label(e.get("hospitalname") or "", e.get("hospitalType"), label, e.get("entrydate"))
            if bank:
                banks.append(bank)
            if not region and e.get("hospitaladd"):
                region = ", ".join(e["hospitaladd"].split(", ")[-2:])
    return StockSnapshot(banks=banks, source="live", fetched_at=time.strftime("%Y-%m-%d %H:%M"),
                         region=region or f"state {state_code} / district {district_code}", groups=list(GROUP_IDS))


def load_stock(prefer_live: bool, state_code: str = DEFAULT_STATE_CODE,
               district_code: str = DEFAULT_DISTRICT_CODE) -> StockSnapshot:
    """Live stock when asked and available, otherwise the snapshot flagged `fallback=True`.

    Never raises for live failures. The router refuses to RECOMMEND on fallback stock.
    """
    if prefer_live:
        try:
            snap = fetch_live(state_code, district_code)
            if snap.banks:
                return snap
            log.warning("e-RaktKosh returned no banks for state %s district %s; using snapshot",
                        state_code, district_code)
        except (requests.RequestException, ValueError) as ex:
            log.warning("e-RaktKosh live fetch failed (%s: %s); using snapshot", type(ex).__name__, ex)
        return load_snapshot().model_copy(update={"fallback": True})
    return load_snapshot()
