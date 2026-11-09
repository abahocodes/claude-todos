"""The one way to change the list: open a transaction, emit events, commit.

    with store.transaction(by=actor(Actor.USER)) as tx:
        tid = tx.create("Send W-9")
        tx.complete(tid, evidence="sent")
"""
import contextlib
import json

from . import events, paths, view
from .lock import atomic_write, locked
from .model import (TODO_FIELDS, Actor, Decision, EventType, ProposalAction, Status, actor,
                    new_event)


def read() -> dict:
    """Current view without locking. Falls back to a rebuild if the cache is missing."""
    try:
        return json.loads(paths.path("todos.json").read_text())
    except (OSError, ValueError):
        return view.build()


class Tx:
    def __init__(self, state: dict, by: str):
        self.state = state
        self.by = by
        self.pending: list = []

    def _emit(self, typ: EventType, todo=None, data=None, evidence=None, by=None):
        ev = new_event(typ, todo, by or self.by, data, evidence)
        view.apply(self.state, ev)  # keep the working view current inside the transaction
        self.pending.append(ev)
        return ev

    def get(self, tid: int) -> dict | None:
        return self.state["todos"].get(str(tid))

    def require(self, tid: int) -> dict:
        t = self.get(tid)
        if t is None:
            raise KeyError(f"no todo #{tid}")
        return t

    def create(self, title: str, evidence=None, by=None, **fields) -> int:
        tid = self.state["next_id"]
        data = {"id": tid, "title": title.strip()}
        data.update({k: v for k, v in fields.items() if v not in (None, [], "")})
        self._emit(EventType.CREATE, tid, data, evidence, by)
        return tid

    def update(self, tid: int, evidence=None, by=None, **fields) -> bool:
        self.require(tid)
        data = {k: v for k, v in fields.items() if k in TODO_FIELDS and v is not None}
        if data:
            self._emit(EventType.UPDATE, tid, data, evidence, by)
        return bool(data)

    def complete(self, tid: int, evidence=None, by=None) -> bool:
        if self.require(tid)["status"] == Status.DONE:
            return False
        self._emit(EventType.COMPLETE, tid, None, evidence, by)
        return True

    def reopen(self, tid: int, evidence=None, by=None) -> bool:
        if self.require(tid)["status"] == Status.OPEN:
            return False
        self._emit(EventType.REOPEN, tid, None, evidence, by)
        return True

    def drop(self, tid: int, reason=None, by=None) -> bool:
        if self.require(tid)["status"] == Status.DROPPED:
            return False
        self._emit(EventType.DROP, tid, {"reason": reason} if reason else None, None, by)
        return True

    def defer(self, tid: int, due=None, by=None) -> None:
        self.require(tid)
        self._emit(EventType.DEFER, tid, {"due": due} if due else None, None, by)

    def propose(self, action: ProposalAction, signal: dict, todo: int | None = None) -> int:
        pid = self.state["next_pid"]
        data = {
            "pid": pid, "action": ProposalAction(action), "todo": todo,
            "title": signal.get("title"), "fields": signal.get("fields") or {},
            "confidence": signal.get("confidence"), "connector": signal.get("connector"),
            "ref": signal.get("ref"),
        }
        self._emit(EventType.PROPOSE, todo, data, signal.get("evidence"),
                   by=actor(Actor.AUTO, signal.get("connector", "?")))
        return pid

    def resolve(self, pid: int, decision: Decision, by=None) -> dict:
        p = self.state["proposals"].get(str(pid))
        if p is None:
            raise KeyError(f"no proposal p{pid}")
        p = dict(p)
        if decision == Decision.APPROVED:
            who, ev_text, todo = by or self.by, p.get("evidence"), p.get("todo")
            match ProposalAction(p["action"]):
                case ProposalAction.CREATE:
                    self.create(p.get("title") or "(untitled)", evidence=ev_text, by=who,
                                source={"connector": p.get("connector"), "ref": p.get("ref")},
                                **p.get("fields", {}))
                case ProposalAction.COMPLETE if self.get(todo):
                    self.complete(todo, evidence=ev_text, by=who)
                case ProposalAction.UPDATE if self.get(todo):
                    self.update(todo, evidence=ev_text, by=who, **p.get("fields", {}))
                case ProposalAction.DROP if self.get(todo):
                    self.drop(todo, reason=ev_text, by=who)
        self._emit(EventType.RESOLVE, p.get("todo"), {"pid": pid, "decision": Decision(decision)}, None, by)
        return p


@contextlib.contextmanager
def transaction(by: str = Actor.USER):
    with locked():
        tx = Tx(view.build(), str(by))
        yield tx
        if tx.pending:
            events.append_events(tx.pending)
            atomic_write(paths.path("todos.json"), json.dumps(tx.state, indent=1))


def rebuild() -> dict:
    with locked():
        state = view.build()
        atomic_write(paths.path("todos.json"), json.dumps(state, indent=1))
        return state
