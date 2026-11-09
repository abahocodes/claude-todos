"""Signals in, decisions out. The only place the "who wins" rules live.

1. Authority.USER (phone app edits, manual signals) applies.
2. Authority.EVIDENCE (merged PR, sent reply) applies unless you edited the todo after the signal.
3. Authority.INFERRED (transcripts, email wording) applies only when auto_apply is on and
   confidence >= confidence_auto; otherwise it becomes a proposal.
Drops from anything but you are always proposals. Nothing is ever deleted.
"""
import difflib
import re
import traceback

from ..core import paths, store
from ..core.lock import locked
from ..core.model import (Actor, Authority, Outcome, ProposalAction, SignalKind, Status, actor,
                          parse_time)


STOPWORDS = {"a", "an", "the", "on", "in", "to", "for", "of", "and", "with", "my", "your", "at", "by"}
WORD = re.compile(r"[a-z0-9]+")


def _words(s: str) -> set[str]:
    return {w for w in WORD.findall(s.lower()) if w not in STOPWORDS}


def _similar(a: str, b: str) -> float:
    """Best of character similarity and word-set overlap, so reordered titles still match:
    'Complete Acme Assessment on HackerRank' == 'Complete Acme HackerRank assessment'."""
    chars = difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()
    wa, wb = _words(a), _words(b)
    words = len(wa & wb) / len(wa | wb) if wa and wb else 0.0
    return max(chars, words)


def best_match(title: str, todos: list[dict]) -> tuple[dict | None, float]:
    best, score = None, 0.0
    for t in todos:
        r = _similar(title, t.get("title", ""))
        if r > score:
            best, score = t, r
    return best, score


def _by(s: dict) -> str:
    return actor(Actor.PHONE if s["authority"] == Authority.USER else Actor.AUTO, s["connector"])


def _pending_dup(tx, action: ProposalAction, todo=None, title=None, ratio=0.85) -> bool:
    for p in tx.state["proposals"].values():
        if p.get("action") != action:
            continue
        if todo is not None and p.get("todo") == todo:
            return True
        if title and p.get("title") and _similar(title, p["title"]) >= ratio:
            return True
    return False


def decide(tx, s: dict, cfg: dict) -> tuple[Outcome, int | None]:
    kind, auth, conf = SignalKind(s["kind"]), Authority(s["authority"]), s["confidence"]
    auto_ok = auth in (Authority.USER, Authority.EVIDENCE) or (
        cfg["auto_apply"] and conf >= cfg["confidence_auto"])
    by, ev = _by(s), s.get("evidence")
    open_todos = [t for t in tx.state["todos"].values() if t["status"] == Status.OPEN]

    if kind == SignalKind.EVIDENCE:  # e.g. a merged PR title: match it to an open todo first
        match, score = best_match(s.get("title", ""), open_todos)
        if not match or score < 0.6:
            return Outcome.UNMATCHED, None
        s = {**s, "kind": SignalKind.DONE, "todo": match["id"], "confidence": min(conf, score),
             "authority": Authority.INFERRED}
        return decide(tx, s, cfg)

    if kind == SignalKind.NEW:
        title = s["title"]
        match, score = best_match(title, open_todos)
        if match and score >= cfg["dedupe_ratio"]:
            return Outcome.DUPLICATE, match["id"]
        if _pending_dup(tx, ProposalAction.CREATE, title=title, ratio=cfg["dedupe_ratio"]):
            return Outcome.DUPLICATE, None
        fields = {k: v for k, v in (s.get("fields") or {}).items() if k != "title"}
        if s.get("due"):
            fields["due"] = s["due"]
        if auto_ok:
            tid = tx.create(title, evidence=ev, by=by,
                            source={"connector": s["connector"], "ref": s.get("ref")}, **fields)
            return Outcome.APPLIED, tid
        tx.propose(ProposalAction.CREATE, {**s, "fields": fields})
        return Outcome.PROPOSED, None

    t = tx.get(s["todo"])
    if t is None:
        return Outcome.UNKNOWN, None
    tid = t["id"]
    if auth != Authority.USER:
        edited, sig_at = parse_time(t.get("last_user_edit")), parse_time(s["at"])
        if edited and sig_at and edited > sig_at:
            return Outcome.SUPERSEDED, tid

    match kind:
        case SignalKind.DONE:
            if t["status"] != Status.OPEN:
                return Outcome.NOOP, tid
            if auto_ok:
                tx.complete(tid, evidence=ev, by=by)
                return Outcome.APPLIED, tid
            if _pending_dup(tx, ProposalAction.COMPLETE, todo=tid):
                return Outcome.DUPLICATE, tid
            tx.propose(ProposalAction.COMPLETE, s, todo=tid)
            return Outcome.PROPOSED, tid

        case SignalKind.REOPEN:
            if auth == Authority.USER and t["status"] != Status.OPEN:
                tx.reopen(tid, evidence=ev, by=by)
                return Outcome.APPLIED, tid
            return Outcome.NOOP, tid

        case SignalKind.UPDATE:
            fields = dict(s.get("fields") or {})
            if s.get("due"):
                fields["due"] = s["due"]
            fields = {k: v for k, v in fields.items() if t.get(k) != v}
            if not fields:
                return Outcome.NOOP, tid
            if auto_ok:
                tx.update(tid, evidence=ev, by=by, **fields)
                return Outcome.APPLIED, tid
            if _pending_dup(tx, ProposalAction.UPDATE, todo=tid):
                return Outcome.DUPLICATE, tid
            tx.propose(ProposalAction.UPDATE, {**s, "fields": fields}, todo=tid)
            return Outcome.PROPOSED, tid

        case SignalKind.DROP:
            if t["status"] != Status.OPEN:
                return Outcome.NOOP, tid
            if auth == Authority.USER:
                tx.drop(tid, reason=ev, by=by)
                return Outcome.APPLIED, tid
            if _pending_dup(tx, ProposalAction.DROP, todo=tid):
                return Outcome.DUPLICATE, tid
            tx.propose(ProposalAction.DROP, s, todo=tid)
            return Outcome.PROPOSED, tid

    return Outcome.IGNORED, None


def apply_signals(signals: list[dict], cfg: dict) -> list[tuple[dict, Outcome, int | None]]:
    if not signals:
        return []
    results = []
    with store.transaction(by=Actor.AUTO) as tx:
        for s in signals:
            outcome, tid = decide(tx, s, cfg)
            results.append((s, outcome, tid))
    _log_results(results)
    return results


def _log_results(results) -> None:
    paths.ensure_dirs()
    with open(paths.path("logs", "decisions.log"), "a") as f:
        for s, outcome, tid in results:
            f.write(f"{s['at']} {s['connector']} {s['kind']} todo={tid} conf={s['confidence']} -> {outcome}"
                    f" | {(s.get('title') or s.get('evidence') or '')[:100]}\n")


# Worker -------------------------------------------------------------------

def run_job(job: dict, cfg: dict) -> None:
    from ..connectors import runner
    from .queue import JobKind

    match JobKind(job["kind"]):
        case JobKind.SESSION:
            runner.run_session(job["target"], cfg)
        case JobKind.CONNECTOR:
            runner.run_connector(job["target"], cfg)


def run_worker(cfg: dict) -> None:
    from . import queue
    from .spawn import WORKER_LOCK

    while True:
        with locked(WORKER_LOCK, blocking=False) as got:
            if not got:
                return
            while True:
                jobs = queue.take_all()
                if not jobs:
                    break
                for job in jobs:
                    try:
                        run_job(job, cfg)
                    except Exception:
                        with open(paths.path("logs", "worker.log"), "a") as f:
                            f.write(f"job {job} failed:\n{traceback.format_exc()}\n")
        if queue.pending() == 0:  # a job queued while we were releasing the lock gets picked up
            return
