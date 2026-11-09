"""Runs every 15 minutes from launchd. Cheap unless there is real work:
- sessions idle for idle_min with unread content (covers walking away, crashes, no SessionEnd)
- connectors whose sync is overdue (covers the Mac having been off)
"""
import datetime as dt
import os

from ..core import state
from ..core.model import parse_time
from . import queue, transcript
from .queue import JobKind, Reason
from .spawn import ensure_worker

FORGET_AFTER_DAYS = 14


def idle_sessions(cfg: dict, now: dt.datetime | None = None) -> list[str]:
    now = now or dt.datetime.now(dt.timezone.utc)
    th = cfg["throttle"]
    out = []
    for sid, s in state.sessions().items():
        path = s.get("transcript")
        if not path or not os.path.exists(path):
            continue
        unread = transcript.size(path) - int(s.get("offset") or 0)
        quiet_s = now.timestamp() - os.path.getmtime(path)
        if unread >= th["idle_min_chars"] and quiet_s >= th["idle_min"] * 60:
            out.append(sid)
    return out


def forget_old_sessions(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    removed = []

    def fn(all_):
        for sid in list(all_):
            s = all_[sid]
            path = s.get("transcript")
            gone = not path or not os.path.exists(path)
            last = parse_time(s.get("last_activity"))
            old = last is None or (now - last).days >= FORGET_AFTER_DAYS
            fully_read = not gone and transcript.size(path) <= int(s.get("offset") or 0)
            if gone or (old and fully_read):
                removed.append(sid)
                del all_[sid]
        return all_

    state.update("sessions", fn)
    return len(removed)


def sweep(cfg: dict) -> dict:
    from ..connectors.runner import due_connectors, refresh_agendas

    agendas = refresh_agendas(cfg) if cfg["alerts"]["enabled"] else {}
    sessions = idle_sessions(cfg)
    for sid in sessions:
        queue.enqueue(JobKind.SESSION, sid, Reason.IDLE)
    connectors = due_connectors(cfg)
    for name in connectors:
        queue.enqueue(JobKind.CONNECTOR, name, Reason.OVERDUE)
    forgotten = forget_old_sessions()
    started = ensure_worker() if (sessions or connectors or queue.pending()) else False
    return {"agendas": agendas, "sessions": sessions, "connectors": connectors, "forgotten": forgotten,
            "worker_started": started}
