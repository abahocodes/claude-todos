"""Runs connectors. All diffing and decisions are shared here; connectors stay thin."""
import datetime as dt
import json
from pathlib import Path

from ..core import agenda, paths, render, state, store
from ..core.model import (Authority, SignalKind, Status, now, parse_time, validate_signal)
from ..engine import llm, reconcile, transcript
from .base import (ConnectorType, Ctx, Role, SecretUnavailable, ctype, discover, is_multi, load_script,
                   resolve, roles)

SESSIONS = "sessions"


# Scheduling ----------------------------------------------------------------

def interval_hours(name: str, m: dict, cfg: dict) -> float:
    return float(cfg["connectors"].get(name, {}).get("every_hours", m.get("every_hours", 24)))


def due_connectors(cfg: dict) -> list[str]:
    """Enabled connectors (other than sessions) whose last successful sync is overdue."""
    from .base import enabled

    out, nowt = [], dt.datetime.now(dt.timezone.utc)
    for name, m in enabled(cfg).items():
        if name == SESSIONS:
            continue
        st = state.connector_state(name)
        retry_after = parse_time(st.get("retry_after"))
        if retry_after and nowt < retry_after:
            continue
        last = parse_time(st.get("last_sync"))
        if last is None or (nowt - last).total_seconds() >= interval_hours(name, m, cfg) * 3600:
            out.append(name)
    return out


def run_connector(name: str, cfg: dict, dry_run: bool = False) -> dict:
    m = resolve(name, cfg)
    if m is None:
        raise KeyError(f"no connector named {name}")
    ctx = Ctx(m, cfg)
    ctx.dry_run = dry_run
    try:
        if Role.REPORTER in roles(m):
            summary = sync_reporter(m, ctx, cfg, dry_run=dry_run)
        elif ctype(m) == ConnectorType.AGENT:
            summary = run_agent_source(m, ctx, cfg, dry_run=dry_run)
        else:
            conn = load_script(m, ctx)
            signals = [validate_signal({**s, "connector": name}) for s in conn.pull()]
            summary = _apply(signals, cfg, dry_run)
        if not dry_run:
            ctx.state["last_sync"] = now()
            ctx.state.pop("last_error", None)
            ctx.state.pop("retry_after", None)
            ctx.save()
        ctx.log(f"sync ok: {summary}")
        return summary
    except Exception as e:
        ctx.state["last_error"] = f"{now()} {type(e).__name__}: {e}"[:400]
        if isinstance(e, SecretUnavailable):  # e.g. 1Password locked: don't prompt every 15 minutes
            ctx.state["retry_after"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
        ctx.save()
        ctx.log(f"sync failed: {e}")
        raise


def _apply(signals: list[dict], cfg: dict, dry_run: bool) -> dict:
    if dry_run:
        return {"signals": signals}
    results = reconcile.apply_signals(signals, cfg)
    counts: dict = {}
    for _, outcome, _ in results:
        counts[str(outcome)] = counts.get(str(outcome), 0) + 1
    return counts


def refresh_agendas(cfg: dict) -> dict:
    """No-AI refresh of upcoming events from script connectors that expose agenda() (ics).
    The sweeper runs this every 15 minutes so in-session alerts stay current."""
    from .base import enabled

    out = {}
    for name, m in enabled(cfg).items():
        if not m.get("agenda") or ctype(m) != ConnectorType.SCRIPT:
            continue
        ctx = Ctx(m, cfg)
        try:
            events = load_script(m, ctx).agenda()
            agenda.save(name, events)
            out[name] = len(events)
        except Exception as e:
            out[name] = f"error: {type(e).__name__}"
            ctx.log(f"agenda refresh failed: {e}")
    return out


# Agent sources (Gmail, Calendar) --------------------------------------------

def _todos_for_prompt(st: dict) -> str:
    lines = [f"#{t['id']} {t['title']}" + (f" (due {t['due']})" if t.get("due") else "")
             for t in render.open_todos(st)]
    recent_done = sorted((t for t in st["todos"].values() if t["status"] == Status.DONE),
                         key=lambda t: t.get("completed", ""), reverse=True)[:10]
    lines += [f"#{t['id']} {t['title']} (ALREADY DONE)" for t in recent_done]
    return "\n".join(lines) or "(none)"


def run_agent_source(m: dict, ctx: Ctx, cfg: dict, dry_run: bool = False) -> dict:
    template = (Path(m["_dir"]) / m.get("prompt", "prompt.md")).read_text()
    lookback = float(m.get("first_lookback_days", 3))
    since = ctx.state.get("since") or (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback)).date().isoformat()
    started = now()
    prompt = (template
              .replace("{since}", since[:10])
              .replace("{today}", started[:10])
              .replace("{todos}", _todos_for_prompt(store.read()))
              .replace("{schema}", llm.SIGNAL_SCHEMA_TEXT))
    obj = llm.run(prompt, cfg, purpose=f"connector:{m['name']}",
                  allowed_tools=m.get("allowed_tools") or [], disallowed_tools=m.get("disallowed_tools"))
    signals, bad = llm.signals_from(obj, m["name"], Authority(m.get("authority", Authority.INFERRED)))
    if m.get("agenda") and isinstance(obj.get("agenda"), list) and not dry_run:
        agenda.save(m["name"], [e for e in obj["agenda"] if isinstance(e, dict)])
    summary = _apply(signals, cfg, dry_run)
    if bad:
        summary["rejected"] = bad
    if not dry_run:
        ctx.state["since"] = started
    return summary


ANALYZE_PROMPT = """{instructions}

Today is {today}. The user's current todo list (ids are what you reference):
{todos}

MATERIAL (fetched read-only by the {name} connector):
{material}

{schema}"""


def analyze_material(ctx: Ctx, instructions: str, material: str) -> list[dict]:
    """Shared by script connectors that fetch text themselves (imap, ics). No tools, no MCP."""
    if not material.strip():
        return []
    signals = []
    for i, part in enumerate(transcript.chunk(material.splitlines(), ctx.cfg["chunk_chars"]), 1):
        prompt = (ANALYZE_PROMPT.replace("{instructions}", instructions)
                  .replace("{today}", now()[:10]).replace("{name}", ctx.name)
                  .replace("{todos}", _todos_for_prompt(store.read()))
                  .replace("{material}", part).replace("{schema}", llm.SIGNAL_SCHEMA_TEXT))
        obj = llm.run(prompt, ctx.cfg, purpose=f"connector:{ctx.name}")
        got, bad = llm.signals_from(obj, ctx.name, Authority(ctx.manifest.get("authority", Authority.INFERRED)))
        if bad:
            ctx.log(f"part {i}: {bad} malformed signals dropped")
        signals += got
    return signals


# Reporters (phone apps): shared two-way sync --------------------------------

def _snap(t: dict) -> dict:
    return {"todo": t["id"], "title": t["title"], "due": t.get("due"), "done": t["status"] == Status.DONE}


def sync_reporter(m: dict, ctx: Ctx, cfg: dict, dry_run: bool = False) -> dict:
    """1. Your edits in the app become USER signals (they win).  2. Local changes are pushed.
    The id map stores a snapshot from the last sync, so each side's changes can be told apart."""
    conn = load_script(m, ctx)
    idmap: dict = ctx.state.setdefault("map", {})
    remote = {str(r["ext_id"]): r for r in conn.list_tasks()}
    name, signals, new_ext = m["name"], [], {}

    for ext, snap in idmap.items():
        r = remote.get(ext)
        base = {"connector": name, "authority": Authority.USER, "confidence": 1.0, "todo": snap["todo"]}
        # Each app-side change also updates the snapshot to what the app now holds,
        # so the push step below only sends changes that started on our side.
        if r is None:  # gone from the open list: completed (or deleted) in the app
            if not snap.get("done"):
                signals.append({**base, "kind": SignalKind.DONE, "evidence": f"completed in {name}"})
                snap["done"] = True
            continue
        if snap.get("done"):
            signals.append({**base, "kind": SignalKind.REOPEN, "evidence": f"reopened in {name}"})
            snap["done"] = False
        fields = {}
        if r.get("title") and r["title"] != snap.get("title"):
            fields["title"] = r["title"]
        if (r.get("due") or None) != (snap.get("due") or None):
            fields["due"] = r.get("due")
        if fields:
            signals.append({**base, "kind": SignalKind.UPDATE, "fields": fields, "evidence": f"edited in {name}"})
            snap.update(fields)
    for ext, r in remote.items():
        if ext not in idmap:
            new_ext[ext] = r
            signals.append({"connector": name, "authority": Authority.USER, "confidence": 1.0,
                            "kind": SignalKind.NEW, "title": r["title"], "due": r.get("due"),
                            "external_id": ext, "evidence": f"added in {name}"})

    signals = [validate_signal(s) for s in signals]
    if dry_run:
        return {"from_app": signals, "remote_open": len(remote), "mapped": len(idmap)}
    summary = {"pulled": _apply(signals, cfg, dry_run=False)}

    st = store.read()
    # map tasks added in the app to the todos just created (or the duplicates they matched)
    for s in signals:
        ext = s.get("external_id")
        if ext:
            match, score = reconcile.best_match(s["title"], list(st["todos"].values()))
            if match and score >= cfg["dedupe_ratio"]:
                idmap[ext] = _snap(match)
    by_todo = {v["todo"]: k for k, v in idmap.items()}

    pushed = {"created": 0, "updated": 0, "completed": 0, "reopened": 0, "deleted": 0, "failed": 0}

    def call(what: str, fn, *args):
        try:
            result = fn(*args)
            pushed[what] += 1
            return result, True
        except Exception as e:  # one bad task never stops the rest of the sync
            pushed["failed"] += 1
            ctx.log(f"{what} failed for {args[0] if args else '?'}: {e}")
            return None, False

    for t in st["todos"].values():
        ext = by_todo.get(t["id"])
        if ext is None:
            if t["status"] == Status.OPEN:
                new_id, ok = call("created", conn.create, t)
                if ok:
                    idmap[str(new_id)] = _snap(t)
            continue
        snap = idmap[ext]
        if t["status"] == Status.DROPPED:
            call("deleted", conn.delete, ext)
            del idmap[ext]
            continue
        is_done = t["status"] == Status.DONE
        ok = True
        if is_done and not snap.get("done"):
            _, ok = call("completed", conn.complete, ext)
        elif not is_done and snap.get("done"):
            _, ok = call("reopened", conn.reopen, ext)
        if ok and (t["title"] != snap.get("title") or (t.get("due") or None) != (snap.get("due") or None)):
            _, ok = call("updated", conn.update, ext, {"title": t["title"], "due": t.get("due")})
        if ok:
            idmap[ext] = _snap(t)

    ctx.state["map"] = idmap
    summary["pushed"] = pushed
    return summary


def remove_reporter_map(name: str) -> None:
    """Archive the id map so a later re-add starts clean without losing the record."""
    s = state.connector_state(name)
    if s:
        state.put(f"archive/{name}-{now().replace(':', '')}", s)
        state.put_connector_state(name, {})


# Sessions (built-in source) --------------------------------------------------

SESSION_PROMPT = """Below is the newest part of a Claude Code work session, and the user's todo list.
Find only real changes to the list:
- "done": a listed open todo was clearly finished in this excerpt (shipped, sent, merged, submitted, confirmed).
- "new": the user committed to a concrete future outcome that is not on the list (not a step already done in this excerpt).
- "update": a listed todo's due date or scope clearly changed.
- "drop": the user clearly abandoned a listed todo.
Track outcomes, not micro-steps. Tool calls and edits alone are not completion. The assistant may already
have updated the list during the session; the list below is current, so never repeat a change it already shows.

TODO LIST:
{todos}

SESSION EXCERPT (part {part} of {parts}):
{excerpt}

{schema}"""


def run_session(session_id: str, cfg: dict) -> dict:
    sess = state.sessions().get(session_id)
    if not sess or not sess.get("transcript"):
        return {"skipped": "unknown session"}
    path, offset = sess["transcript"], int(sess.get("offset") or 0)
    if transcript.size(path) < offset:  # transcript was rewritten; start over
        offset = 0
    lines, new_offset, bad = transcript.read_delta(path, offset)
    summary: dict = {"lines": len(lines), "bad_lines": bad}
    signals = []
    chunks = transcript.chunk(lines, cfg["chunk_chars"]) if lines else []
    for i, ch in enumerate(chunks, 1):
        prompt = (SESSION_PROMPT.replace("{todos}", _todos_for_prompt(store.read()))
                  .replace("{part}", str(i)).replace("{parts}", str(len(chunks)))
                  .replace("{excerpt}", ch).replace("{schema}", llm.SIGNAL_SCHEMA_TEXT))
        obj = llm.run(prompt, cfg, purpose="session")  # on failure the offset is not advanced
        got, rejected = llm.signals_from(obj, SESSIONS, Authority.INFERRED)
        for s in got:
            s["ref"] = f"session:{session_id}"
        signals += got
        summary["rejected"] = summary.get("rejected", 0) + rejected
    summary["decisions"] = _apply(signals, cfg, dry_run=False) if signals else {}

    def advance(all_):
        s = all_.setdefault(session_id, {})
        s["offset"] = new_offset
        s["last_check"] = now()
        return all_

    state.update("sessions", advance)
    return summary


def status_rows(cfg: dict) -> list[dict]:
    """Every connector you can use: single ones, multi templates, and your named instances."""
    rows = []
    instances = {n: c["template"] for n, c in cfg["connectors"].items() if c.get("template")}
    for name, m in discover().items():
        multi = is_multi(m)
        rows.append(_row(name, m, cfg, template=multi))
        for inst, tmpl in sorted(instances.items()):
            if multi and tmpl == name:
                rows.append(_row(inst, {**m, "name": inst}, cfg, of=name))
    return rows


def _row(name: str, m: dict, cfg: dict, template: bool = False, of: str | None = None) -> dict:
    st = state.connector_state(name)
    settings = cfg["connectors"].get(name, {})
    about = m.get("description", "")
    if template:
        about = f"template: add one per account with `todo connector add {name} --as <name> ...`. {about}"
    elif of:
        shown = {k: v for k, v in settings.items() if k not in ("enabled", "template")}
        about = f"{of} instance {shown}"
    return {
        "name": name, "roles": sorted(m.get("roles") or []), "type": m.get("type", "script"),
        "enabled": bool(settings.get("enabled")) and not template, "template": template,
        "last_sync": None if name == SESSIONS else st.get("last_sync"), "last_error": st.get("last_error"),
        "mapped": len(st.get("map", {})) or None, "about": about,
        "per_session": name == SESSIONS,
    }


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, default=str)
