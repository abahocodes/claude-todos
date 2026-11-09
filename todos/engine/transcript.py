"""Read only the new part of a Claude Code transcript (JSONL), compactly.

Rules learned from prior art:
- stop at the last complete line, so a half-written line is read next time, not skipped
- drop tool results (most of the bulk), keep short tool-call signatures
- never truncate: split into chunks so nothing is marked read without being seen
The transcript format is undocumented, so anything unrecognised is skipped, never fatal.
"""
import json
import os
import re

REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
MAX_TEXT = 1500


def size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _brief(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "pattern", "url", "query", "description", "skill"):
        if inp.get(k):
            return str(inp[k])[:80]
    return ""


def _text_of(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                out.append(b["text"])
            elif b.get("type") == "tool_use":
                out.append(f"[tool: {b.get('name', '?')}({_brief(b.get('input'))})]")
    return out


def entry_lines(obj: dict) -> list[str]:
    if obj.get("isMeta") or obj.get("isSidechain"):
        return []
    role = obj.get("type")
    if role not in ("user", "assistant"):
        return []
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return []
    lines = []
    for t in _text_of(msg.get("content")):
        t = REMINDER.sub("", t).strip()
        if not t:
            continue
        if len(t) > MAX_TEXT:
            t = t[:MAX_TEXT // 2] + " ... " + t[-MAX_TEXT // 2:]
        lines.append(f"{role.upper()}: {t}")
    return lines


def read_delta(path: str, offset: int) -> tuple[list[str], int, int]:
    """Returns (lines, new_offset, bad_lines). new_offset sits right after the last full line."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset, 0
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset, 0
    complete = data[: end + 1]
    lines, bad = [], 0
    for raw in complete.splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            bad += 1
            continue
        if isinstance(obj, dict):
            lines.extend(entry_lines(obj))
    return lines, offset + len(complete), bad


def chunk(lines: list[str], max_chars: int) -> list[str]:
    chunks, cur, n = [], [], 0
    for ln in lines:
        if cur and n + len(ln) > max_chars:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks
