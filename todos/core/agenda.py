"""Upcoming calendar events with real times, shared by every calendar connector, read by the hooks.

Each source replaces its own slice: state/agenda.json = {source: {"updated": iso, "events": [...]}}.
An event is {"start": iso, "end": iso|None, "title": str, "source": str, "all_day": bool}.
"""
import datetime as dt
from enum import StrEnum

from . import state
from .model import Status, now, parse_time

STALE_AFTER_H = 36  # ignore a source that hasn't refreshed in this long


class AlertKind(StrEnum):
    SOON = "soon"        # starts within lead_min
    NOW = "now"          # starts within now_min, or already underway
    DUE_TODAY = "due"    # todos due today / overdue, once per session per day


def save(source: str, events: list[dict]) -> None:
    clean = []
    for e in events:
        start = parse_time(e.get("start"))
        if not start or not e.get("title"):
            continue
        end = parse_time(e.get("end"))
        clean.append({"start": start.isoformat(), "end": end.isoformat() if end else None,
                      "title": str(e["title"])[:150], "source": source, "all_day": bool(e.get("all_day"))})

    def fn(all_):
        all_[source] = {"updated": now(), "events": sorted(clean, key=lambda x: parse_time(x["start"]))}
        return all_

    state.update("agenda", fn)


def events(now_t: dt.datetime | None = None) -> list[dict]:
    """All fresh events, merged across sources, with the same event from two calendars shown once."""
    now_t = now_t or dt.datetime.now(dt.timezone.utc)
    seen, out = set(), []
    for src in state.get("agenda", {}).values():
        updated = parse_time(src.get("updated"))
        if updated is None or (now_t - updated).total_seconds() > STALE_AFTER_H * 3600:
            continue
        for e in src.get("events", []):
            key = (e["title"].strip().lower(), parse_time(e["start"]).astimezone(dt.timezone.utc).isoformat()[:16])
            if key not in seen:
                seen.add(key)
                out.append(e)
    return sorted(out, key=lambda e: parse_time(e["start"]))


def _local(iso: str) -> dt.datetime:
    return parse_time(iso).astimezone()


def _when(e: dict) -> str:
    return "all day" if e.get("all_day") else _local(e["start"]).strftime("%-I:%M%p").lower()


def today(now_t: dt.datetime | None = None) -> list[dict]:
    now_t = now_t or dt.datetime.now(dt.timezone.utc)
    d = now_t.astimezone().date()
    return [e for e in events(now_t) if _local(e["start"]).date() == d
            and (e.get("all_day") or (parse_time(e.get("end")) or _local(e["start"])) >= now_t)]


def today_lines(now_t: dt.datetime | None = None) -> list[str]:
    return [f"- {_when(e)} {e['title']} ({e['source']})" for e in today(now_t)]


def alerts(cfg: dict, todos_state: dict, already: set, now_t: dt.datetime | None = None) -> tuple[list[str], set]:
    """Alert lines not yet shown in this session, and the keys to remember."""
    now_t = now_t or dt.datetime.now(dt.timezone.utc)
    a = cfg["alerts"]
    lines, keys = [], set()
    for e in events(now_t):
        if e.get("all_day"):
            continue
        start = parse_time(e["start"])
        end = parse_time(e.get("end")) or start + dt.timedelta(minutes=30)
        mins = (start - now_t).total_seconds() / 60
        if end <= now_t or mins > a["lead_min"]:
            continue
        kind = AlertKind.NOW if mins <= a["now_min"] else AlertKind.SOON
        key = f"{kind}|{e['title']}|{e['start']}"
        if key in already:
            continue
        keys.add(key)
        if mins > 0:
            lines.append(f"{e['title']} starts in {int(mins + 0.5)} min ({_when(e)}, {e['source']})")
        else:
            lines.append(f"{e['title']} started {int(-mins + 0.5)} min ago ({_when(e)}, {e['source']})")
    day = now_t.astimezone().date()
    due_key = f"{AlertKind.DUE_TODAY}|{day.isoformat()}"
    if due_key not in already:
        due = [t for t in todos_state["todos"].values() if t["status"] == Status.OPEN and t.get("due")
               and str(t["due"])[:10] <= day.isoformat()]
        if due:
            keys.add(due_key)
            lines.append("Due today or overdue: " + "; ".join(f"#{t['id']} {t['title']} (due {t['due']})" for t in due[:5]))
    return lines, keys
