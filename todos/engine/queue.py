"""Job queue: one file per job key, so repeated triggers for the same session merge into one job."""
import json
import os
import re
from enum import StrEnum

from ..core import paths
from ..core.lock import atomic_write, locked
from ..core.model import now

SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class JobKind(StrEnum):
    SESSION = "session"      # target = session id
    CONNECTOR = "connector"  # target = connector name


class Reason(StrEnum):
    STOP = "stop"
    PRE_COMPACT = "pre-compact"
    SESSION_END = "session-end"
    IDLE = "idle"
    OVERDUE = "overdue"
    MANUAL = "manual"


def _job_path(key: str):
    return paths.path("jobs", SAFE.sub("_", key) + ".json")


def enqueue(kind: JobKind, target: str, reason: Reason, **extra) -> None:
    paths.ensure_dirs()
    kind, reason = JobKind(kind), Reason(reason)
    key = f"{kind}-{target}"
    with locked(".queue.lock"):
        p = _job_path(key)
        try:
            job = json.loads(p.read_text())
        except (OSError, ValueError):
            job = {"kind": kind, "target": target, "reasons": [], "queued": now()}
        if reason not in job["reasons"]:
            job["reasons"].append(reason)
        job.update(extra)
        atomic_write(p, json.dumps(job))


def take_all() -> list[dict]:
    jobs = []
    with locked(".queue.lock"):
        d = paths.path("jobs")
        if not d.exists():
            return []
        for p in sorted(d.glob("*.json")):
            try:
                jobs.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                pass
            try:
                os.remove(p)
            except OSError:
                pass
    return jobs


def pending() -> int:
    d = paths.path("jobs")
    return len(list(d.glob("*.json"))) if d.exists() else 0
