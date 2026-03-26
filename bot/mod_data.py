"""Moderation record storage and helpers."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

DATA_FILE = Path(__file__).parent.parent / "data" / "mod_data.json"
AI_TRAINING_FILE = Path(__file__).parent.parent / "data" / "ai_training.json"


# ── Mod records ───────────────────────────────────────────────────────────────

def load_mod() -> dict:
    if not DATA_FILE.exists():
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        return {"records": {}, "appeals": {}}
    try:
        with DATA_FILE.open() as f:
            data = json.load(f)
        data.setdefault("records", {})
        data.setdefault("appeals", {})
        return data
    except (json.JSONDecodeError, ValueError):
        default = {"records": {}, "appeals": {}}
        with DATA_FILE.open("w") as f:
            json.dump(default, f, indent=2)
        return default


def save_mod(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def add_record(
    user_id: int,
    action: str,
    reason: str,
    mod: str,
    duration: Optional[str],
) -> None:
    data = load_mod()
    data["records"].setdefault(str(user_id), []).append({
        "action": action,
        "reason": reason,
        "mod": mod,
        "duration": duration,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    })
    save_mod(data)


def parse_duration(duration: str) -> Optional[datetime.timedelta]:
    """Parse a duration string like '10m', '2h', '3d', '1w', 'permanent'."""
    low = duration.lower()
    if low in ("perm", "permanent", "none", "-"):
        return None
    if low.endswith("mo"):
        try:
            return datetime.timedelta(days=int(low[:-2]) * 30)
        except ValueError:
            return None
    if low.endswith("y"):
        try:
            return datetime.timedelta(days=int(low[:-1]) * 365)
        except ValueError:
            return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = low[-1]
    if unit not in units:
        return None
    try:
        return datetime.timedelta(seconds=int(low[:-1]) * units[unit])
    except ValueError:
        return None


# ── AI training data ──────────────────────────────────────────────────────────

def load_training() -> list:
    if not AI_TRAINING_FILE.exists():
        return []
    try:
        with AI_TRAINING_FILE.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []


def save_training(data: list) -> None:
    AI_TRAINING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with AI_TRAINING_FILE.open("w") as f:
        json.dump(data, f, indent=2)
