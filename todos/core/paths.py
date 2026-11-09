"""Where data lives and how config is loaded. Override the data folder with TODOS_HOME."""
import copy
import json
import os
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TODO_BIN = PLUGIN_ROOT / "bin" / "todo"

DEFAULT_CONFIG = {
    # Inferred changes at or above this confidence apply on their own once auto_apply is on.
    "confidence_auto": 0.8,
    # Start in proposals-only mode; flip after reviewing a week of proposals.
    "auto_apply": False,
    "stale_days": 14,
    "rethink_deferrals": 3,
    "context_max_todos": 15,
    "model": "haiku",
    "llm_timeout_s": 180,
    "llm_budget_usd": 0.25,
    "throttle": {
        "min_chars": 2000,       # unread transcript bytes before a Stop-triggered check
        "min_interval_min": 30,  # minimum time between checks of one session
        "idle_min": 10,          # sweeper: session quiet this long gets checked
        "idle_min_chars": 400,   # sweeper: ignore tiny leftovers
    },
    "chunk_chars": 12000,
    "alerts": {
        "enabled": True,
        "lead_min": 30,  # alert when an event starts within this many minutes
        "now_min": 5,    # second, louder alert when it's this close (or underway)
    },
    "dedupe_ratio": 0.85,
    "connectors": {"sessions": {"enabled": True}},
}


def home() -> Path:
    return Path(os.environ.get("TODOS_HOME") or "~/.claude/todos").expanduser()


def path(*parts: str) -> Path:
    return home().joinpath(*parts)


def ensure_dirs() -> None:
    for sub in ("", "state", "state/connectors", "state/archive", "jobs", "logs", "connectors"):
        path(sub).mkdir(parents=True, exist_ok=True)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    p = path("config.json")
    try:
        user = json.loads(p.read_text())
    except (OSError, ValueError):
        user = {}
    return _merge(DEFAULT_CONFIG, user)


def save_config(cfg: dict) -> None:
    from .lock import atomic_write

    atomic_write(path("config.json"), json.dumps(cfg, indent=2) + "\n")
