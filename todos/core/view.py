"""Fold the event log into the current list (todos.json) and pending proposals."""
from .events import read_events
from .model import TODO_FIELDS, EventType, Status, is_auto

HISTORY_KEEP = 20


def empty() -> dict:
    return {"todos": {}, "proposals": {}, "next_id": 1, "next_pid": 1, "events": 0, "last_event": None}


def _touch(t: dict, ev: dict, change: str) -> None:
    t["updated"] = ev["at"]
    t["last_touched"] = ev["at"]
    if not is_auto(ev["by"]):
        t["last_user_edit"] = ev["at"]
    entry = {"at": ev["at"], "by": ev["by"], "change": change}
    if ev.get("evidence"):
        entry["evidence"] = ev["evidence"]
    t.setdefault("history", []).append(entry)
    del t["history"][:-HISTORY_KEEP]


def apply(state: dict, ev: dict) -> None:
    try:
        typ = EventType(ev.get("type"))
    except ValueError:
        return  # an event type from a newer version: skip, never crash
    tid, data = ev.get("todo"), ev.get("data") or {}
    todos = state["todos"]
    key = str(tid) if tid is not None else None
    state["events"] += 1
    state["last_event"] = ev.get("id")

    if typ == EventType.CREATE:
        t = {
            "id": tid, "title": data.get("title", "(untitled)"), "status": Status.OPEN,
            "notes": data.get("notes"), "due": data.get("due"), "parent": data.get("parent"),
            "labels": data.get("labels") or [], "source": data.get("source"),
            "created": ev["at"], "deferrals": 0,
        }
        todos[key] = t
        state["next_id"] = max(state["next_id"], tid + 1)
        _touch(t, ev, "created")
        return

    if typ == EventType.PROPOSE:
        pid = data.get("pid")
        if pid is not None:
            state["proposals"][str(pid)] = {**data, "at": ev["at"], "by": ev["by"], "evidence": ev.get("evidence")}
            state["next_pid"] = max(state["next_pid"], pid + 1)
        return
    if typ == EventType.RESOLVE:
        state["proposals"].pop(str(data.get("pid")), None)
        return

    t = todos.get(key)
    if t is None:
        return
    match typ:
        case EventType.UPDATE:
            changed = [f for f in data if f in TODO_FIELDS]
            for f in changed:
                t[f] = data[f]
            _touch(t, ev, "updated " + ",".join(changed))
        case EventType.COMPLETE:
            t["status"] = Status.DONE
            t["completed"] = ev["at"]
            _touch(t, ev, "done")
        case EventType.REOPEN:
            t["status"] = Status.OPEN
            t.pop("completed", None)
            _touch(t, ev, "reopened")
        case EventType.DROP:
            t["status"] = Status.DROPPED
            _touch(t, ev, "dropped" + (f": {data['reason']}" if data.get("reason") else ""))
        case EventType.DEFER:
            t["deferrals"] = t.get("deferrals", 0) + 1
            if "due" in data:
                t["due"] = data["due"]
            _touch(t, ev, "deferred")


def build() -> dict:
    state = empty()
    for ev in read_events():
        apply(state, ev)
    return state
