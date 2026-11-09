"""Claude Code hook handlers. Rule one: never break or slow the session.
Every handler is wrapped: errors are logged and the hook exits 0 with no output."""
import datetime as dt
import json
import os
import sys
import traceback
from enum import StrEnum

from .core import agenda, paths, render, state, store
from .core.events import read_events
from .core.model import Actor, EventType, now, parse_time
from .engine import queue, transcript
from .engine.queue import JobKind, Reason
from .engine.spawn import RECURSION_ENV, ensure_worker


class HookEvent(StrEnum):
    SESSION_START = "session-start"
    PROMPT = "prompt"
    STOP = "stop"
    PRE_COMPACT = "pre-compact"
    SESSION_END = "session-end"


CLAUDE_EVENT_NAME = {
    HookEvent.SESSION_START: "SessionStart",
    HookEvent.PROMPT: "UserPromptSubmit",
}


def _emit_context(event: HookEvent, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": CLAUDE_EVENT_NAME[event], "additionalContext": text}}))


def _read_input() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except (OSError, ValueError):
        return {}


VISIBLE_CHANGES = {EventType.CREATE: "added", EventType.COMPLETE: "done", EventType.REOPEN: "reopened",
                   EventType.DROP: "dropped", EventType.UPDATE: "edited", EventType.PROPOSE: "proposed"}


def _changes_since(seen: int | None, st: dict) -> list[str]:
    """Changes made outside Claude sessions (phone app, background checks) after event number `seen`."""
    if seen is None or seen >= st["events"]:
        return []
    out = []
    for i, ev in enumerate(read_events()):
        if i < seen:
            continue
        who = ev.get("by", "").split(":", 1)[0]
        verb = VISIBLE_CHANGES.get(ev.get("type"))
        if verb and who in (Actor.PHONE, Actor.AUTO):
            t = st["todos"].get(str(ev.get("todo")), {})
            label = t.get("title") or (ev.get("data") or {}).get("title") or "?"
            out.append(f"#{ev.get('todo', '-')} {label}: {verb} ({ev['by']})")
    return out[-5:]


ALERT_HEADER = ("TIME-SENSITIVE (from the todos plugin): tell the user this in one short line at the very "
                "top of your reply, before anything else:")


def _alerts(sid: str, st: dict, cfg: dict) -> list[str]:
    """New alerts for this session; each one fires once per session."""
    if not cfg["alerts"]["enabled"]:
        return []
    shown = set(state.sessions().get(sid, {}).get("alerted", []))
    lines, keys = agenda.alerts(cfg, st, shown)
    if keys:
        state.touch_session(sid, None, alerted=sorted(shown | keys)[-200:])
    return lines


def on_session_start(inp: dict, cfg: dict) -> None:
    sid = inp.get("session_id")
    st = store.read()
    if sid:
        state.touch_session(sid, inp.get("transcript_path"), seen=st["events"])
    from .connectors.runner import due_connectors  # catch-up after the Mac was off

    overdue = due_connectors(cfg)
    for name in overdue:
        queue.enqueue(JobKind.CONNECTOR, name, Reason.OVERDUE)
    if overdue:
        ensure_worker()
    parts = [render.session_context(st, cfg)]
    if cfg["alerts"]["enabled"]:
        day = agenda.today_lines()
        if day:
            parts.append("\n## Rest of today's calendar\n" + "\n".join(day))
        alerts = _alerts(sid, st, cfg) if sid else []
        if alerts:
            parts.append("\n" + ALERT_HEADER + "\n" + "\n".join(f"- {a}" for a in alerts))
    _emit_context(HookEvent.SESSION_START, "\n".join(parts))


def on_prompt(inp: dict, cfg: dict) -> None:
    sid = inp.get("session_id")
    if not sid:
        return
    st = store.read()
    prev = state.sessions().get(sid, {}).get("seen")
    state.touch_session(sid, inp.get("transcript_path"), seen=st["events"])
    parts = []
    alerts = _alerts(sid, st, cfg)
    if alerts:
        parts.append(ALERT_HEADER + "\n" + "\n".join(f"- {a}" for a in alerts))
    changes = _changes_since(prev if isinstance(prev, int) else None, st)
    if changes:
        parts.append("Todo list changed outside this session: " + "; ".join(changes))
    if parts:
        _emit_context(HookEvent.PROMPT, "\n".join(parts))


def _queue_session(inp: dict, reason: Reason) -> None:
    sid = inp.get("session_id")
    if not sid:
        return
    state.touch_session(sid, inp.get("transcript_path"))
    queue.enqueue(JobKind.SESSION, sid, reason)
    ensure_worker()


def on_stop(inp: dict, cfg: dict) -> None:
    sid, path = inp.get("session_id"), inp.get("transcript_path")
    if not sid or not path:
        return
    s = state.touch_session(sid, path)
    th = cfg["throttle"]
    unread = transcript.size(path) - int(s.get("offset") or 0)
    if unread < th["min_chars"]:
        return
    last = parse_time(s.get("last_check"))
    elapsed_min = None if last is None else (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
    if elapsed_min is not None and elapsed_min < th["min_interval_min"]:
        return  # the sweeper picks it up once the session goes quiet
    queue.enqueue(JobKind.SESSION, sid, Reason.STOP)
    ensure_worker()


def run(event_name: str) -> int:
    if os.environ.get(RECURSION_ENV):
        return 0  # our own background claude -p runs must never trigger us
    try:
        event = HookEvent(event_name)
        inp = _read_input()
        cfg = paths.load_config()
        match event:
            case HookEvent.SESSION_START:
                on_session_start(inp, cfg)
            case HookEvent.PROMPT:
                on_prompt(inp, cfg)
            case HookEvent.STOP:
                on_stop(inp, cfg)
            case HookEvent.PRE_COMPACT:
                _queue_session(inp, Reason.PRE_COMPACT)
            case HookEvent.SESSION_END:
                sid = inp.get("session_id")
                if sid:
                    state.touch_session(sid, inp.get("transcript_path"), ended=now())
                _queue_session(inp, Reason.SESSION_END)
    except Exception:
        try:
            paths.ensure_dirs()
            with open(paths.path("logs", "hooks.log"), "a") as f:
                f.write(f"{now()} {event_name} failed:\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0
