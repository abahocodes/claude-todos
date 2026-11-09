"""Append-only event log: the master copy. Nothing else is authoritative."""
import json

from . import paths


def log_path():
    return paths.path("events.jsonl")


def read_events():
    p = log_path()
    if not p.exists():
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue  # a torn line never blocks the rest of the history


def append_events(events: list) -> None:
    """Caller must hold the store lock."""
    if not events:
        return
    paths.ensure_dirs()
    with open(log_path(), "a") as f:
        for ev in events:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        f.flush()
