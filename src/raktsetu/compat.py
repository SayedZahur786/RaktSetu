"""ABO/Rh red-cell compatibility. Pure functions, no I/O.

Owns: which donor groups a recipient group can receive, which substitutes need the
clinical note's permission, and parsing free-text blood-group names.
"""
from __future__ import annotations

import re

from .models import BloodGroup

GROUPS: tuple[BloodGroup, ...] = ("O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+")

# Red-cell compatibility: recipient -> donor groups it can safely receive, best match first.
_DONORS: dict[BloodGroup, tuple[BloodGroup, ...]] = {
    "O-": ("O-",),
    "O+": ("O+", "O-"),
    "A-": ("A-", "O-"),
    "A+": ("A+", "A-", "O+", "O-"),
    "B-": ("B-", "O-"),
    "B+": ("B+", "B-", "O+", "O-"),
    "AB-": ("AB-", "A-", "B-", "O-"),
    "AB+": ("AB+", "AB-", "A+", "A-", "B+", "B-", "O+", "O-"),
}


def compatible_donors(recipient: BloodGroup) -> tuple[BloodGroup, ...]:
    """Donor groups a `recipient` can always receive, exact group first."""
    return _DONORS[recipient]


def safe_substitutes(requested: BloodGroup) -> tuple[BloodGroup, ...]:
    """Groups other than `requested` that are compatible with no extra information."""
    return tuple(g for g in _DONORS[requested] if g != requested)


def conditional_substitutes(requested: BloodGroup) -> tuple[BloodGroup, ...]:
    """Groups that become compatible only if the patients are in fact Rh-positive.

    Requests often ask for Rh-negative (especially O-negative) out of habit. If the note
    says the patients are Rh-positive, the Rh-positive donors open up. Whether the note
    says so is a judgment for the decision model, never for this table.
    """
    if requested.endswith("+"):
        return ()
    rh_pos: BloodGroup = requested[:-1] + "+"  # type: ignore[assignment]
    strict = set(_DONORS[requested])
    return tuple(g for g in _DONORS[rh_pos] if g not in strict)


# ABO letters must be upper-case so "a negative crossmatch" is never read as A-negative.
_GROUP_RE = re.compile(
    r"\b((?-i:AB|A|B|O))\s*[-\s]?\s*"
    r"(\+|-|pos(?:itive)?|neg(?:ative)?|\+ve|-ve|ve\+|ve-)",
    re.IGNORECASE,
)


def parse_group(text: str) -> BloodGroup | None:
    """Find the first blood group in free text: 'O-negative', 'O neg', 'O-Ve', 'AB+' ...

    Returns None when no group (or more than one distinct group) is mentioned, so an
    ambiguous note never silently picks a group.
    """
    found: set[str] = set()
    for abo, rh in _GROUP_RE.findall(text):
        rh_l = rh.lower()
        sign = "+" if ("+" in rh_l or rh_l.startswith("pos")) else "-"
        found.add(abo.upper() + sign)
    if len(found) != 1:
        return None
    group = found.pop()
    return group if group in _DONORS else None  # type: ignore[return-value]


def from_eraktkosh(label: str) -> BloodGroup | None:
    """Map e-RaktKosh labels like 'O-Ve' / 'AB+Ve' to 'O-' / 'AB+'."""
    m = re.fullmatch(r"\s*(AB|A|B|O)\s*([+-])\s*Ve\s*", label, re.IGNORECASE)
    if not m:
        return None
    return (m.group(1).upper() + m.group(2))  # type: ignore[return-value]
