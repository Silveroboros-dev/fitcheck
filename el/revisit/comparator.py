"""Pure comparison of one retained market capture with an activation baseline."""

from __future__ import annotations

from datetime import date
from hashlib import sha256

POLICY_VERSION = "manual-revisit-v0.1"


def _digest(value: object) -> str | None:
    return sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else None


def _normalized(value: str) -> str:
    return " ".join(value.split())


def compare_market_evidence(
    baseline: dict, later: dict, *, market_id: str, today: date
) -> dict:
    """Return evidence codes, never a semantic thesis or resolution verdict.

    The required v0 fields are title, rules, deadline and lifecycle. Missing
    historical fields stay unknown; a changed known field may still propose a
    packet. An outcome is never inferred from a deadline or a closed flag.
    """
    before = {key: baseline.get(key) for key in ("title", "rules", "deadline", "lifecycle", "condition_id")}
    after = {key: later.get(key) for key in ("title", "rules", "deadline", "lifecycle", "condition_id")}
    unknowns: list[str] = []
    triggers: list[str] = []
    unchanged: list[str] = []
    if (baseline.get("market_id") != market_id or later.get("market_id") != market_id
        or (before["condition_id"] is not None and after["condition_id"] is not None
            and before["condition_id"] != after["condition_id"])):
        unknowns.append("market_identity_mismatch")
    else:
        if before["condition_id"] is None:
            unknowns.append("baseline_condition_unknown")
        elif after["condition_id"] is None:
            unknowns.append("later_condition_unknown")
        else:
            unchanged.append("condition_unchanged")
        for field in ("title", "rules"):
            old, new = before[field], after[field]
            if not isinstance(old, str) or not old.strip():
                unknowns.append(f"baseline_{field}_unknown")
            elif not isinstance(new, str) or not new.strip():
                unknowns.append(f"later_{field}_unknown")
            elif _normalized(old) != _normalized(new):
                triggers.append(f"{field}_changed")
            else:
                unchanged.append(f"{field}_unchanged")

        old_deadline, new_deadline = before["deadline"], after["deadline"]
        if not old_deadline:
            unknowns.append("baseline_deadline_unknown")
        elif not new_deadline:
            unknowns.append("later_deadline_unknown")
        else:
            try:
                old_date = date.fromisoformat(old_deadline)
                new_date = date.fromisoformat(new_deadline)
            except (TypeError, ValueError):
                unknowns.append("deadline_invalid")
            else:
                if old_date != new_date:
                    triggers.append("deadline_changed")
                else:
                    unchanged.append("deadline_unchanged")
                # Date-only deadlines are not known to have passed at 00:00.
                if today > new_date:
                    triggers.append("deadline_reached")

        old_lifecycle, new_lifecycle = before["lifecycle"], after["lifecycle"]
        if old_lifecycle == "resolved" and not baseline.get("resolution_evidence"):
            old_lifecycle = None
        if new_lifecycle == "resolved" and not later.get("resolution_evidence"):
            new_lifecycle = None
        if old_lifecycle not in ("open", "closed", "resolved"):
            unknowns.append("baseline_lifecycle_unknown")
        elif new_lifecycle not in ("open", "closed", "resolved"):
            unknowns.append("later_lifecycle_unknown")
        elif old_lifecycle != new_lifecycle:
            triggers.append("lifecycle_changed")
        else:
            unchanged.append("lifecycle_unchanged")

    if "market_identity_mismatch" in unknowns:
        triggers.clear()
        unchanged.clear()
    result = "revisit_proposed" if triggers else "incomplete" if unknowns else "no_material_change"
    return {
        "policy_version": POLICY_VERSION,
        "result": result,
        "triggers": sorted(set(triggers)),
        "unknowns": sorted(set(unknowns)),
        "unchanged": sorted(set(unchanged)),
        "before": {**before, "title_hash": _digest(before["title"]), "rules_hash": _digest(before["rules"])},
        "after": {**after, "title_hash": _digest(after["title"]), "rules_hash": _digest(after["rules"])},
        "resolution_outcome": None,
    }
