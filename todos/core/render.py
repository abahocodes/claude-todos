"""Compact text for hook context, the CLI and the skill. Short lines keep context cheap."""
from . import paths, rules
from .model import ProposalAction, Status


def todo_line(t: dict, cfg: dict) -> str:
    bits = [f"#{t['id']} {t['title']}"]
    if t.get("due"):
        bits.append(f"due {t['due']}")
    f = rules.flags(t, cfg)
    if f:
        bits.append("[" + ", ".join(f) + "]")
    if t.get("parent"):
        bits.append(f"(under #{t['parent']})")
    if t.get("status") != Status.OPEN:
        bits.append(f"<{t['status']}>")
    return "  ".join(bits)


def proposal_line(p: dict) -> str:
    what = {
        ProposalAction.CREATE: f"add \"{p.get('title')}\"",
        ProposalAction.COMPLETE: f"mark #{p.get('todo')} done",
        ProposalAction.UPDATE: f"update #{p.get('todo')} {p.get('fields')}",
        ProposalAction.DROP: f"drop #{p.get('todo')}",
    }.get(p.get("action"), p.get("action"))
    due = (p.get("fields") or {}).get("due")
    if due and p.get("action") == ProposalAction.CREATE:
        what += f" due {due}"
    ev = f" | \"{(p.get('evidence') or '')[:120]}\"" if p.get("evidence") else ""
    return f"p{p['pid']}: {what} ({p.get('connector')}, {p.get('confidence')}){ev}"


def open_todos(state: dict) -> list[dict]:
    items = [t for t in state["todos"].values() if t.get("status") == Status.OPEN]
    return sorted(items, key=rules.sort_key)


def listing(state: dict, cfg: dict, limit: int | None = None, include_closed: bool = False) -> str:
    items = list(state["todos"].values()) if include_closed else open_todos(state)
    if include_closed:
        items.sort(key=lambda t: (t.get("status") != Status.OPEN, -int(t["id"])))
    total = len(items)
    items = items[:limit] if limit else items
    lines = [todo_line(t, cfg) for t in items]
    if limit and total > limit:
        lines.append(f"... {total - limit} more (todo list)")
    return "\n".join(lines) if lines else "(no open todos)"


def session_context(state: dict, cfg: dict) -> str:
    todo = str(paths.TODO_BIN)
    props = list(state["proposals"].values())
    parts = [
        "## Todos (from the todos plugin)",
        listing(state, cfg, limit=cfg["context_max_todos"]),
    ]
    if props:
        parts.append(f"\nPending proposals ({len(props)}), ask the user to approve or reject when relevant:")
        parts += [proposal_line(p) for p in props[:5]]
    parts.append(
        "\nKeep this list current as you work, without being asked:\n"
        f"- Finished something on the list: `{todo} done <id> --by claude --evidence \"<what proves it>\"`\n"
        f"- The user commits to new work worth tracking: `{todo} add \"<title>\" --by claude`\n"
        f"- Plans change: `{todo} edit <id> --title/--due ...` or `{todo} drop <id> --reason ...`\n"
        "Track outcomes, not micro-steps. Mention the change in one short line. "
        "For reasoning about the list (stale, rethink, break down), use the /todos skill."
    )
    return "\n".join(parts)
