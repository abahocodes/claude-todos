"""Connector contract and registry.

A connector is a folder holding connector.json, plus, by ConnectorType:
  - script:  <module>.py defining `Connector(ctx)` (Source and/or Reporter methods below)
  - agent:   prompt.md, run through headless claude with only the listed read-only tools
  - builtin: implemented inside the plugin (sessions)

Built-in connectors live in this package; your own go in ~/.claude/todos/connectors/<name>/.

A manifest with "multi": true is a template (imap, ics): add one named instance per account with
`todo connector add imap --as work-mail --set host=... --set user=...`. Each instance has its own
settings, secret (Keychain item 'todos-<instance>') and sync state.

Role.SOURCE:    pull() -> list[signal dict]   (script sources may use ctx.analyze() to read text)
Role.REPORTER:  list_tasks() -> list[{"ext_id", "title", "due"}]   (open tasks only)
                create(todo) -> ext_id ; update(ext_id, fields) ; complete(ext_id)
                reopen(ext_id) ; delete(ext_id)
A reporter is also a source: the shared sync (runner.sync_reporter) turns your edits in the
app into signals, so a reporter only implements those calls.
"""
import importlib.util
import json
import os
import subprocess
from enum import StrEnum
from pathlib import Path

from ..core import paths, state
from ..core.model import now

BUILTIN_DIR = Path(__file__).parent


class Role(StrEnum):
    SOURCE = "source"
    REPORTER = "reporter"


class ConnectorType(StrEnum):
    SCRIPT = "script"
    AGENT = "agent"
    BUILTIN = "builtin"


class Ctx:
    """What a connector gets: its manifest, config, saved state, a logger and its secret."""

    def __init__(self, manifest: dict, cfg: dict):
        self.manifest = manifest
        self.name = manifest["name"]
        self.cfg = cfg
        self.settings = cfg["connectors"].get(self.name, {})
        self.state = state.connector_state(self.name)
        self.dry_run = False

    def save(self) -> None:
        state.put_connector_state(self.name, self.state)

    def secret(self) -> str | None:
        """In order: the manifest's environment variable; a 1Password reference in the setting
        `secret_ref` (op://Vault/Item/field); the macOS Keychain item 'todos-<name>'."""
        env = (self.manifest.get("auth") or {}).get("env")
        if env and os.environ.get(env):
            return os.environ[env]
        ref = self.settings.get("secret_ref")
        if ref:
            return op_read(ref)
        return keychain_read(f"todos-{self.name}")

    def log(self, msg: str) -> None:
        paths.ensure_dirs()
        with open(paths.path("logs", "connectors.log"), "a") as f:
            f.write(f"{now()} [{self.name}] {msg}\n")

    def analyze(self, instructions: str, material: str) -> list[dict]:
        """Shared helper for script connectors that fetch text themselves (mail, calendar feeds):
        Haiku reads `material` against the current list and returns validated signals."""
        from .runner import analyze_material

        return analyze_material(self, instructions, material)


OP_TOKEN_ITEM = "todos-op-service-account"


class SecretUnavailable(RuntimeError):
    """The secret exists but can't be read right now (e.g. 1Password locked). Retry next sync."""


def keychain_read(service: str) -> str | None:
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def op_read(ref: str) -> str:
    """Read a 1Password reference. Uses a service account token from the Keychain when present
    (never prompts, right for background runs); otherwise the 1Password app integration."""
    env = dict(os.environ)
    token = env.get("OP_SERVICE_ACCOUNT_TOKEN") or keychain_read(OP_TOKEN_ITEM)
    if token:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    try:
        out = subprocess.run(["op", "read", "--no-newline", ref], capture_output=True, text=True,
                             timeout=60, env=env, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise SecretUnavailable("1Password CLI (op) is not installed")
    except subprocess.TimeoutExpired:
        raise SecretUnavailable("1Password did not answer in time (locked, or waiting for Touch ID)")
    if out.returncode != 0:
        raise SecretUnavailable(f"1Password could not read {ref}: {out.stderr.strip()[:200]}")
    return out.stdout


def discover() -> dict[str, dict]:
    """name -> manifest (with '_dir'). Your connectors override built-ins of the same name."""
    found = {}
    for root in (BUILTIN_DIR, paths.path("connectors")):
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            mf = d / "connector.json"
            if not mf.is_file():
                continue
            try:
                m = json.loads(mf.read_text())
            except ValueError:
                continue
            m.setdefault("name", d.name)
            m["_dir"] = str(d)
            found[m["name"]] = m
    return found


def roles(m: dict) -> set[Role]:
    return {Role(r) for r in m.get("roles") or []}


def ctype(m: dict) -> ConnectorType:
    return ConnectorType(m.get("type", ConnectorType.SCRIPT))


def is_multi(m: dict) -> bool:
    """A multi connector (imap, ics) is a template: you add named instances of it, one per account."""
    return bool(m.get("multi"))


def resolve(name: str, cfg: dict) -> dict | None:
    """A connector by name: a built-in/your own connector, or an instance of a multi template."""
    all_ = discover()
    inst = cfg["connectors"].get(name) or {}
    template = inst.get("template")
    if template:
        base = all_.get(template)
        if base is None or not is_multi(base):
            return None
        return {**base, "name": name, "template": template}
    m = all_.get(name)
    return None if m is None or is_multi(m) else m


def load_script(m: dict, ctx: Ctx):
    mod_file = Path(m["_dir"]) / m.get("module", m["name"] + ".py")
    spec = importlib.util.spec_from_file_location(f"todos_connector_{m['name']}", mod_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Connector(ctx)


def enabled(cfg: dict) -> dict[str, dict]:
    out = {}
    for n, c in cfg["connectors"].items():
        if c.get("enabled"):
            m = resolve(n, cfg)
            if m is not None:
                out[n] = m
    return out
