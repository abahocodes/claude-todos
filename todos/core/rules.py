"""Heuristic flags the skill and hooks share: stale, rethink, break down, overdue."""
import datetime as dt
import re
from enum import StrEnum

from .model import Status, parse_time

MULTI_STEP = re.compile(r"\b(and then|then|;|, and)\b|\s\+\s", re.I)


class Flag(StrEnum):
    OVERDUE = "overdue"
    STALE = "stale"
    RETHINK = "rethink"
    BREAK_DOWN = "break down"


def _days_since(ts: str | None, now: dt.datetime) -> float | None:
    t = parse_time(ts)
    return None if t is None else (now - t).total_seconds() / 86400


def _due_date(todo: dict) -> dt.date | None:
    d = todo.get("due")
    if not d:
        return None
    try:
        return dt.date.fromisoformat(str(d)[:10])
    except ValueError:
        return None


def flags(todo: dict, cfg: dict, now: dt.datetime | None = None) -> list[str]:
    """Labels like 'overdue 3d', 'stale 20d', 'rethink', 'break down'. Each starts with a Flag value."""
    if todo.get("status") != Status.OPEN:
        return []
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    due = _due_date(todo)
    if due and due < now.date():
        out.append(f"{Flag.OVERDUE} {(now.date() - due).days}d")
    idle = _days_since(todo.get("last_touched"), now)
    if idle is not None and idle >= cfg["stale_days"]:
        out.append(f"{Flag.STALE} {int(idle)}d")
    if todo.get("deferrals", 0) >= cfg["rethink_deferrals"] or (due and (now.date() - due).days > 7):
        out.append(str(Flag.RETHINK))
    title = todo.get("title", "")
    if len(title.split()) > 14 or MULTI_STEP.search(title):
        out.append(str(Flag.BREAK_DOWN))
    return out


def sort_key(todo: dict, now: dt.datetime | None = None):
    """Overdue first, then by due date, then most recently touched."""
    now = now or dt.datetime.now(dt.timezone.utc)
    due = _due_date(todo)
    touched = parse_time(todo.get("last_touched")) or now
    return (0 if due and due < now.date() else 1, due or dt.date.max, -touched.timestamp())
