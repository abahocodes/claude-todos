"""Shapes of events and signals, their enums, and validation.

Enums are StrEnums: they serialise to plain strings, so the JSON files stay readable and
values read back from disk compare equal to the enum members.
"""
import datetime as dt
import secrets
from enum import StrEnum


class EventType(StrEnum):
    CREATE = "create"      # data: title, notes?, due?, parent?, labels?, source?
    UPDATE = "update"      # data: any of TODO_FIELDS
    COMPLETE = "complete"
    REOPEN = "reopen"
    DROP = "drop"          # data: reason?
    DEFER = "defer"        # data: due?
    PROPOSE = "propose"    # data: pid, action, todo?, fields, confidence, connector, ref
    RESOLVE = "resolve"    # data: pid, decision


class Status(StrEnum):
    OPEN = "open"
    DONE = "done"
    DROPPED = "dropped"


class SignalKind(StrEnum):
    NEW = "new"
    DONE = "done"
    UPDATE = "update"
    DROP = "drop"
    REOPEN = "reopen"
    EVIDENCE = "evidence"  # e.g. a merged PR title, matched to an open todo


class Authority(StrEnum):
    USER = "user"          # you, directly (phone app edit, manual signal)
    EVIDENCE = "evidence"  # hard proof (merged PR, sent reply)
    INFERRED = "inferred"  # a model's reading of transcripts or email


class ProposalAction(StrEnum):
    CREATE = "create"
    COMPLETE = "complete"
    UPDATE = "update"
    DROP = "drop"


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class Outcome(StrEnum):
    APPLIED = "applied"
    PROPOSED = "proposed"
    DUPLICATE = "duplicate"
    SUPERSEDED = "superseded"  # you edited the todo after the signal happened
    NOOP = "noop"
    UNKNOWN = "unknown"        # signal names a todo that doesn't exist
    UNMATCHED = "unmatched"    # evidence matched no open todo
    IGNORED = "ignored"


class Actor(StrEnum):
    """Prefixes for an event's `by` field. Anything not AUTO counts as your own edit."""
    USER = "user"
    CLAUDE = "claude"
    PHONE = "phone"
    AUTO = "auto"


TODO_FIELDS = ("title", "notes", "due", "parent", "labels")


def actor(kind: Actor, detail: str | None = None) -> str:
    return f"{kind}:{detail}" if detail else str(kind)


def is_auto(by: str) -> bool:
    return by.split(":", 1)[0] == Actor.AUTO


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def new_event(type_: EventType, todo: int | None, by: str, data: dict | None = None,
              evidence: str | None = None, at: str | None = None) -> dict:
    ev = {"id": "e_" + secrets.token_hex(5), "at": at or now(), "type": EventType(type_), "by": by}
    if todo is not None:
        ev["todo"] = todo
    if data:
        ev["data"] = data
    if evidence:
        ev["evidence"] = evidence[:500]
    return ev


def validate_signal(s: dict) -> dict:
    """Normalise a signal from any connector. Raises ValueError on garbage."""
    if not isinstance(s, dict):
        raise ValueError("signal is not an object")
    kind = SignalKind(s.get("kind"))  # ValueError on an unknown kind
    try:
        authority = Authority(s.get("authority"))
    except ValueError:
        authority = Authority.INFERRED
    out = {
        "connector": str(s.get("connector") or "unknown"),
        "kind": kind,
        "authority": authority,
        "confidence": max(0.0, min(1.0, float(s.get("confidence", 0.5)))),
        "at": s.get("at") or now(),
    }
    todo = s.get("todo")
    if todo is not None:
        try:
            out["todo"] = int(str(todo).lstrip("#t"))
        except ValueError:
            raise ValueError(f"bad todo id {todo!r}")
    for k in ("title", "ref", "evidence", "notes", "due", "external_id"):
        if s.get(k):
            out[k] = str(s[k])[:500]
    if isinstance(s.get("fields"), dict):
        out["fields"] = {k: v for k, v in s["fields"].items() if k in TODO_FIELDS}
    if kind == SignalKind.NEW and not out.get("title"):
        raise ValueError("new signal needs a title")
    if kind in (SignalKind.DONE, SignalKind.UPDATE, SignalKind.DROP, SignalKind.REOPEN) and "todo" not in out:
        raise ValueError(f"{kind} signal needs a todo id")
    return out
