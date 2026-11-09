"""Small JSON state files: per-session transcript offsets, per-connector cursors and id maps."""
import json
import os

from . import paths
from .lock import atomic_write, locked


def _file(name: str):
    return paths.path("state", name + ".json")


def get(name: str, default=None):
    try:
        return json.loads(_file(name).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def put(name: str, value) -> None:
    atomic_write(_file(name), json.dumps(value, indent=1))


def update(name: str, fn, default=None):
    """Read-modify-write under the state lock. fn mutates or returns a new value."""
    with locked(".state.lock"):
        cur = get(name, default)
        new = fn(cur)
        put(name, cur if new is None else new)
        return cur if new is None else new


# Sessions ----------------------------------------------------------------

def sessions() -> dict:
    return get("sessions", {})


def touch_session(session_id: str, transcript: str | None, **extra) -> dict:
    from .model import now

    def fn(all_):
        if session_id not in all_:
            # First sight of a session that may already be long (resumed, or running when the
            # plugin was installed): start at the current end so history isn't re-analysed.
            try:
                start = os.path.getsize(transcript) if transcript else 0
            except OSError:
                start = 0
            all_[session_id] = {"offset": start, "last_check": None, "first_seen": now()}
        s = all_[session_id]
        if transcript:
            s["transcript"] = transcript
        s["last_activity"] = now()
        s.update(extra)
        return all_

    return update("sessions", fn)[session_id]


def connector_state(name: str) -> dict:
    return get("connectors/" + name, {})


def put_connector_state(name: str, value: dict) -> None:
    put("connectors/" + name, value)
