"""`todo` command line. Thin: parse arguments, call the shared library, print short output."""
import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .core import paths, render, rules, state, store
from .core.model import Actor, Decision, Status, actor, validate_signal

LAUNCHD_LABEL = "com.todos.sweep"
LAUNCHD_PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCHD_LABEL}.plist"


def _ids(values: list[str]) -> list[int]:
    return [int(v.lstrip("#tp")) for v in values]


def _by(args) -> str:
    try:
        return actor(Actor(args.by.split(":", 1)[0]), args.by.split(":", 1)[1] if ":" in args.by else None)
    except ValueError:
        raise SystemExit(f"--by must start with one of: {', '.join(a.value for a in Actor)}")


# CRUD ------------------------------------------------------------------------

def cmd_add(args, cfg):
    with store.transaction(by=_by(args)) as tx:
        parent = _ids([args.parent])[0] if args.parent else None
        tid = tx.create(args.title, evidence=args.evidence, due=args.due, notes=args.notes,
                        parent=parent, labels=args.label or None)
        print(f"added #{tid} {args.title}")


def cmd_edit(args, cfg):
    fields = {k: getattr(args, k) for k in ("title", "due", "notes") if getattr(args, k) is not None}
    if args.parent is not None:
        fields["parent"] = _ids([args.parent])[0] if args.parent else None
    if args.label is not None:
        fields["labels"] = args.label
    with store.transaction(by=_by(args)) as tx:
        tid = _ids([args.id])[0]
        tx.update(tid, evidence=args.evidence, **fields)
        print(f"updated #{tid}")


def _bulk(args, verb: str, fn_name: str, **kw):
    with store.transaction(by=_by(args)) as tx:
        for tid in _ids(args.ids):
            changed = getattr(tx, fn_name)(tid, **kw)
            print(f"{verb} #{tid} {tx.require(tid)['title']}" + ("" if changed is not False else " (no change)"))


def cmd_done(args, cfg):
    _bulk(args, "done", "complete", evidence=args.evidence)


def cmd_reopen(args, cfg):
    _bulk(args, "reopened", "reopen", evidence=args.evidence)


def cmd_drop(args, cfg):
    _bulk(args, "dropped", "drop", reason=args.reason)


def cmd_defer(args, cfg):
    with store.transaction(by=_by(args)) as tx:
        for tid in _ids(args.ids):
            tx.defer(tid, due=args.due)
            t = tx.require(tid)
            print(f"deferred #{tid} (now {t['deferrals']}x" + (f", due {t['due']})" if t.get("due") else ")"))


def cmd_list(args, cfg):
    st = store.read()
    if args.json:
        items = list(st["todos"].values()) if args.all else render.open_todos(st)
        print(json.dumps([{**t, "flags": rules.flags(t, cfg)} for t in items], indent=1))
    else:
        print(render.listing(st, cfg, limit=args.limit, include_closed=args.all))


def cmd_show(args, cfg):
    t = store.read()["todos"].get(str(_ids([args.id])[0]))
    if not t:
        raise SystemExit(f"no todo #{args.id}")
    print(render.todo_line(t, cfg))
    for k in ("notes", "source", "created", "last_touched", "deferrals"):
        if t.get(k):
            print(f"  {k}: {t[k]}")
    for h in t.get("history", [])[-10:]:
        print(f"  {h['at'][:16]} {h['by']}: {h['change']}" + (f" | {h['evidence'][:100]}" if h.get("evidence") else ""))


def cmd_review(args, cfg):
    st = store.read()
    flagged = [(t, rules.flags(t, cfg)) for t in render.open_todos(st)]
    flagged = [(t, f) for t, f in flagged if f]
    print(f"{len(render.open_todos(st))} open, {len(flagged)} flagged, {len(st['proposals'])} proposals")
    for t, _ in flagged:
        print(render.todo_line(t, cfg))
    for p in st["proposals"].values():
        print(render.proposal_line(p))


def cmd_proposals(args, cfg):
    props = store.read()["proposals"].values()
    print("\n".join(render.proposal_line(p) for p in props) or "(no proposals)")


def _resolve(args, decision: Decision):
    with store.transaction(by=_by(args)) as tx:
        pids = list(tx.state["proposals"]) if args.all else [str(i) for i in _ids(args.pids)]
        for pid in pids:
            p = tx.resolve(int(pid), decision)
            print(f"{decision} p{pid}: {render.proposal_line(p).split(': ', 1)[1]}")


def cmd_approve(args, cfg):
    _resolve(args, Decision.APPROVED)


def cmd_reject(args, cfg):
    _resolve(args, Decision.REJECTED)


def cmd_signal(args, cfg):
    """Feed a signal by hand or from your own scripts: todo signal '{"kind":"new","title":"..."}'"""
    from .engine import reconcile

    raw = json.loads(args.json if args.json != "-" else sys.stdin.read())
    signals = [validate_signal({"connector": "manual", **s}) for s in (raw if isinstance(raw, list) else [raw])]
    for s, outcome, tid in reconcile.apply_signals(signals, cfg):
        print(f"{s['kind']} -> {outcome}" + (f" (#{tid})" if tid else ""))


def cmd_log(args, cfg):
    p = paths.path("logs", "decisions.log")
    lines = p.read_text().splitlines()[-args.limit:] if p.exists() else []
    print("\n".join(lines) or "(no decisions yet)")


def cmd_agenda(args, cfg):
    from .connectors.runner import refresh_agendas
    from .core import agenda

    if args.refresh:
        print(json.dumps(refresh_agendas(cfg)))
    evs = agenda.events()
    if not args.week:
        evs = agenda.today()
    for e in evs:
        when = agenda._local(e["start"]).strftime("%a %b %-d ") + agenda._when(e)
        print(f"{when:22} {e['title']}  ({e['source']})")
    if not evs:
        print("(nothing on the calendar" + (" this week)" if args.week else " for the rest of today)"))


def cmd_context(args, cfg):
    print(render.session_context(store.read(), cfg))


def cmd_rebuild(args, cfg):
    st = store.rebuild()
    print(f"rebuilt from {st['events']} events: {len(st['todos'])} todos, {len(st['proposals'])} proposals")


# Engine ----------------------------------------------------------------------

def cmd_hook(args, cfg):
    from .hooks import run

    return run(args.event)


def cmd_reconcile(args, cfg):
    from .engine import queue, reconcile
    from .engine.queue import JobKind, Reason

    if args.session:
        queue.enqueue(JobKind.SESSION, args.session, Reason.MANUAL)
    reconcile.run_worker(cfg)
    if not args.worker:
        print("reconcile finished; see `todo log`")


def cmd_sweep(args, cfg):
    from .engine.sweep import sweep

    print(json.dumps(sweep(cfg)))


def cmd_stats(args, cfg):
    p = paths.path("logs", "llm.jsonl")
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
    by: dict = {}
    for r in rows:
        b = by.setdefault(r.get("purpose", "?"), {"calls": 0, "cost_usd": 0.0, "errors": 0, "in": 0, "out": 0})
        b["calls"] += 1
        b["cost_usd"] += r.get("cost_usd") or 0
        b["errors"] += 1 if r.get("error") else 0
        b["in"] += r.get("input_tokens") or 0
        b["out"] += r.get("output_tokens") or 0
    for k, b in sorted(by.items()):
        print(f"{k:22} calls={b['calls']:4}  ${b['cost_usd']:.3f}  in={b['in']:,} out={b['out']:,}  errors={b['errors']}")
    if not rows:
        print("(no AI calls yet)")
        return
    print(f"total ${sum(b['cost_usd'] for b in by.values()):.3f} over {len(rows)} calls")
    print("Amounts are API-equivalent. Logged in with a Claude subscription, they count toward "
          "your usage limits instead of being billed; with ANTHROPIC_API_KEY they are billed.")


# Connectors ------------------------------------------------------------------

def _sync_label(r: dict) -> str:
    if r["template"]:
        return "template"
    if r["per_session"]:
        return "runs per session"
    return f"last sync {(r['last_sync'] or 'never')[:16]}"


def _store_secret(name: str, op_token: bool = False) -> None:
    import getpass

    if op_token:
        name = "op-service-account"
        prompt = "1Password service account token (input hidden): "
    else:
        prompt = f"Secret for {name} (password, app password or feed URL; input hidden): "
    value = getpass.getpass(prompt).strip()
    if not value:
        raise SystemExit("nothing entered; secret unchanged")
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", f"todos-{name}", "-a", "todos", "-w", value],
                       capture_output=True, text=True)
    print(f"stored in Keychain as todos-{name}" if r.returncode == 0 else f"Keychain error: {r.stderr.strip()}")


def _oauth(name: str, args, cfg) -> None:
    """One-time browser sign-in for connectors whose manifest declares "oauth"."""
    from .connectors import oauth
    from .connectors.base import resolve

    m = resolve(name, cfg)
    if m is None or not m.get("oauth"):
        raise SystemExit(f"{name} doesn't use OAuth sign-in")
    if not args.client:
        raise SystemExit("pass --client <path to the downloaded OAuth client JSON ('Desktop app' type)>")
    blob = oauth.consent(oauth.load_client(os.path.expanduser(args.client)), m["oauth"]["scopes"],
                         login_hint=args.login_hint)
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", f"todos-{name}", "-a", "todos",
                        "-w", json.dumps(blob)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Keychain error: {r.stderr.strip()}")
    print(f"signed in; token stored in Keychain as todos-{name}. Next: todo connector add {name}")


def cmd_connector(args, cfg):
    from .connectors import runner
    from .connectors.base import Role, discover, is_multi, resolve, roles

    if args.action == "list":
        for r in runner.status_rows(cfg):
            mark = " - " if r["template"] else ("on " if r["enabled"] else "off")
            indent = "  " if "instance" in r["about"] else ""
            print(f"[{mark}] {indent}{r['name']:12} {'+'.join(r['roles']):16} {r['type']:7} {_sync_label(r)}"
                  + (f"  mapped {r['mapped']}" if r["mapped"] else "")
                  + (f"\n      error: {r['last_error']}" if r["last_error"] else "")
                  + (f"\n      {r['about']}" if r["about"] else ""))
        return
    if args.action == "secret" and args.op_token:
        _store_secret("", op_token=True)
        return
    name = args.name
    if not name:
        raise SystemExit("name a connector")
    if args.action == "secret":
        _store_secret(name)
        return
    if args.action == "auth":
        _oauth(name, args, cfg)
        return

    if args.action == "add":
        all_ = discover()
        if name not in all_:
            raise SystemExit(f"no connector named {name}; see `todo connector list`")
        target = name
        if is_multi(all_[name]):
            if not args.as_:
                raise SystemExit(f"{name} is a template: add an instance, e.g. `todo connector add {name} --as personal --set user=...`")
            target = args.as_
            if target in all_:
                raise SystemExit(f"'{target}' is already a connector name; pick another")
            cfg["connectors"].setdefault(target, {})["template"] = name
        settings = cfg["connectors"].setdefault(target, {})
        for kv in args.set or []:
            k, _, v = kv.partition("=")
            try:
                settings[k] = json.loads(v)
            except ValueError:
                settings[k] = v
        settings["enabled"] = True
        paths.save_config(_user_config(cfg))
        m = resolve(target, cfg)
        print(f"enabled {target}. First sync now"
              + (" (pushes your open todos to the app)" if Role.REPORTER in roles(m) else ""))
        try:
            print(runner.dumps(runner.run_connector(target, cfg)))
        except Exception as e:
            print(f"first sync failed: {e}\nIt stays enabled; fix that and run `todo connector sync {target}`.")
        return

    m = resolve(name, cfg)
    if m is None:
        raise SystemExit(f"no connector named {name}; see `todo connector list`")
    if args.action == "remove":
        cfg["connectors"].setdefault(name, {})["enabled"] = False
        paths.save_config(_user_config(cfg))
        if Role.REPORTER in roles(m):
            runner.remove_reporter_map(name)
        print(f"disabled {name}. Its history stays in the event log"
              + ("; its id map is archived." if Role.REPORTER in roles(m) else ".")
              + (f" Keychain item todos-{name} is kept; delete it with "
                 f"`security delete-generic-password -s todos-{name}`." if m.get("template") else ""))
    elif args.action == "sync":
        print(runner.dumps(runner.run_connector(name, cfg, dry_run=args.dry_run)))


def _user_config(cfg: dict) -> dict:
    """Only persist what differs from defaults, so default changes in new versions still apply."""
    def diff(a, b):
        out = {}
        for k, v in a.items():
            if isinstance(v, dict) and isinstance(b.get(k), dict):
                d = diff(v, b[k])
                if d:
                    out[k] = d
            elif b.get(k) != v:
                out[k] = v
        return out

    return diff(cfg, paths.DEFAULT_CONFIG)


def cmd_config(args, cfg):
    if args.key is None:
        print(json.dumps(cfg, indent=1))
        return
    keys = args.key.split(".")
    if args.value is None:
        cur = cfg
        for k in keys:
            cur = cur[k]
        print(json.dumps(cur))
        return
    cur = cfg
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    try:
        cur[keys[-1]] = json.loads(args.value)
    except ValueError:
        cur[keys[-1]] = args.value
    paths.save_config(_user_config(cfg))
    print(f"{args.key} = {json.dumps(cur[keys[-1]])}")


# Install ---------------------------------------------------------------------

def cmd_launchd(args, cfg):
    if args.action == "install":
        paths.ensure_dirs()
        plist = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [shutil.which("python3") or sys.executable, str(paths.TODO_BIN), "sweep"],
            "StartInterval": 900,
            "RunAtLoad": True,
            "StandardOutPath": str(paths.path("logs", "sweep.log")),
            "StandardErrorPath": str(paths.path("logs", "sweep.log")),
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "TODOS_HOME": str(paths.home())},
        }
        LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        LAUNCHD_PLIST.write_bytes(plistlib.dumps(plist))
        r = subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)], capture_output=True, text=True)
        print(f"installed {LAUNCHD_PLIST} (every 15 min)" if r.returncode == 0 else f"launchctl failed: {r.stderr}")
    elif args.action == "uninstall":
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        LAUNCHD_PLIST.unlink(missing_ok=True)
        print("sweeper removed")
    else:
        r = subprocess.run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True, text=True)
        print("sweeper loaded" if r.returncode == 0 else "sweeper not loaded")


def cmd_doctor(args, cfg):
    from .connectors.runner import status_rows
    from .engine import queue
    from .engine.spawn import worker_running

    st = store.read()
    checks = [
        ("data folder", str(paths.home()), paths.home().exists()),
        ("claude CLI", shutil.which("claude") or "not on PATH", bool(shutil.which("claude"))),
        ("events", f"{st['events']} events, {len(render.open_todos(st))} open", True),
        ("proposals", str(len(st["proposals"])), True),
        ("auto_apply", str(cfg["auto_apply"]), True),
        ("sessions tracked", str(len(state.sessions())), True),
        ("jobs queued", str(queue.pending()), True),
        ("worker running", str(worker_running()), True),
        ("sweeper", "loaded" if subprocess.run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True).returncode == 0
         else "not loaded (todo launchd install)", True),
    ]
    for name, val, ok in checks:
        print(f"{'ok ' if ok else 'BAD'} {name:16} {val}")
    for r in status_rows(cfg):
        if r["enabled"]:
            print(f"{'BAD' if r['last_error'] else 'ok '} connector {r['name']:12} {_sync_label(r)}"
                  + (f" | {r['last_error']}" if r["last_error"] else ""))
    hl = paths.path("logs", "hooks.log")
    if hl.exists() and hl.stat().st_size:
        print(f"note: hook errors logged in {hl}")


# Parser ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="todo", description="Todos shared across Claude Code sessions.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_by(sp, default=Actor.USER):
        sp.add_argument("--by", default=str(default), help="who made the change: user, claude, ...")
        return sp

    a = with_by(sub.add_parser("add", help="add a todo"))
    a.add_argument("title")
    for f in ("--due", "--notes", "--parent", "--evidence"):
        a.add_argument(f)
    a.add_argument("--label", action="append")
    a.set_defaults(fn=cmd_add)

    e = with_by(sub.add_parser("edit", help="change a todo's fields"))
    e.add_argument("id")
    for f in ("--title", "--due", "--notes", "--parent", "--evidence"):
        e.add_argument(f)
    e.add_argument("--label", action="append")
    e.set_defaults(fn=cmd_edit)

    for name, fn, extra in (("done", cmd_done, "--evidence"), ("reopen", cmd_reopen, "--evidence"),
                            ("drop", cmd_drop, "--reason")):
        sp = with_by(sub.add_parser(name))
        sp.add_argument("ids", nargs="+")
        sp.add_argument(extra)
        sp.set_defaults(fn=fn)

    d = with_by(sub.add_parser("defer", help="push a todo back (counts toward 'rethink')"))
    d.add_argument("ids", nargs="+")
    d.add_argument("--due")
    d.set_defaults(fn=cmd_defer)

    ls = sub.add_parser("list", help="open todos")
    ls.add_argument("--all", action="store_true", help="include done and dropped")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--limit", type=int)
    ls.set_defaults(fn=cmd_list)

    sh = sub.add_parser("show")
    sh.add_argument("id")
    sh.set_defaults(fn=cmd_show)

    sub.add_parser("review", help="flagged todos + proposals").set_defaults(fn=cmd_review)
    sub.add_parser("proposals").set_defaults(fn=cmd_proposals)
    for name, fn in (("approve", cmd_approve), ("reject", cmd_reject)):
        sp = with_by(sub.add_parser(name))
        sp.add_argument("pids", nargs="*")
        sp.add_argument("--all", action="store_true")
        sp.set_defaults(fn=fn)

    sg = sub.add_parser("signal", help="feed a signal JSON (or - for stdin)")
    sg.add_argument("json")
    sg.set_defaults(fn=cmd_signal)

    lg = sub.add_parser("log", help="recent automatic decisions")
    lg.add_argument("--limit", type=int, default=30)
    lg.set_defaults(fn=cmd_log)

    ag = sub.add_parser("agenda", help="upcoming events the alerts use (today; --week for 7 days)")
    ag.add_argument("--week", action="store_true")
    ag.add_argument("--refresh", action="store_true", help="re-read calendar feeds first")
    ag.set_defaults(fn=cmd_agenda)

    sub.add_parser("context", help="what sessions see at start").set_defaults(fn=cmd_context)
    sub.add_parser("rebuild", help="rebuild todos.json from the event log").set_defaults(fn=cmd_rebuild)
    sub.add_parser("stats", help="AI call counts and cost").set_defaults(fn=cmd_stats)
    sub.add_parser("doctor", help="health check").set_defaults(fn=cmd_doctor)

    h = sub.add_parser("hook", help="(used by Claude Code hooks)")
    h.add_argument("event")
    h.set_defaults(fn=cmd_hook)

    r = sub.add_parser("reconcile", help="process queued jobs now")
    r.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--session", help="queue this session id first")
    r.set_defaults(fn=cmd_reconcile)

    sub.add_parser("sweep", help="queue idle sessions + overdue syncs (launchd runs this)").set_defaults(fn=cmd_sweep)

    c = sub.add_parser("connector", help="list | add | remove | sync | secret | auth")
    c.add_argument("action", choices=["list", "add", "remove", "sync", "secret", "auth"])
    c.add_argument("--client", help="auth: OAuth client JSON downloaded from Google Cloud Console")
    c.add_argument("--login-hint", help="auth: which Google account to preselect")
    c.add_argument("name", nargs="?")
    c.add_argument("--as", dest="as_", help="add: instance name for a multi connector (imap, ics)")
    c.add_argument("--set", action="append", help="add: setting key=value (repeatable)")
    c.add_argument("--dry-run", action="store_true", help="sync: show signals without applying")
    c.add_argument("--op-token", action="store_true",
                   help="secret: store a 1Password service account token for background syncs")
    c.set_defaults(fn=cmd_connector)

    cf = sub.add_parser("config", help="show or set config: todo config throttle.min_chars 3000")
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    cf.set_defaults(fn=cmd_config)

    ld = sub.add_parser("launchd", help="install | uninstall | status the 15-min sweeper")
    ld.add_argument("action", choices=["install", "uninstall", "status"])
    ld.set_defaults(fn=cmd_launchd)
    return p


def main(argv: list[str] | None = None) -> int:
    from .core.net import prefer_ipv4

    prefer_ipv4()
    args = build_parser().parse_args(argv)
    cfg = paths.load_config()
    try:
        rc = args.fn(args, cfg)
    except KeyError as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    return rc or 0
