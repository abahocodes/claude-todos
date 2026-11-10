"""GitHub source: your merged pull requests since the last sync, read by the model next to the open
todos, so a PR links to the todo it finishes by meaning (title, body, repo), not by title spelling.
Only "done" signals come back; they stay inferred, so a merged PR always becomes a proposal."""
import datetime as dt
import json
import subprocess

from todos.core.model import SignalKind

INSTRUCTIONS = """These are pull requests the user authored that were merged recently: repo, title, URL, then the PR body.
- "done" only when a PR clearly delivers what a listed open todo asks for. Link by meaning: the PR may
  use different words than the todo (a conventional-commit title, a ticket number, a component name).
- Nothing else: no "new", "update" or "drop" signals. A PR that matches no open todo produces nothing.
- Quote the PR title in "evidence" and put the PR URL in "ref". At most 10 signals."""

BODY_CHARS = 1500
SEEN_KEEP = 500


def summarize(pr: dict) -> str:
    body = " ".join((pr.get("body") or "").split())[:BODY_CHARS]
    repo = (pr.get("repository") or {}).get("nameWithOwner", "?")
    return f"[{repo}] {pr['title']} ({pr['url']})\n  {body or '(no description)'}"


class Connector:
    def __init__(self, ctx):
        self.ctx = ctx

    def pull(self) -> list[dict]:
        since = self.ctx.state.get("since") or (dt.date.today() - dt.timedelta(days=3)).isoformat()
        out = subprocess.run(
            ["gh", "search", "prs", "--author", "@me", "--merged", f"--merged-at=>={since[:10]}",
             "--json", "title,url,body,repository,closedAt", "--limit", "50"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            raise RuntimeError(f"gh search failed: {out.stderr.strip()[:200]}")
        seen = set(self.ctx.state.get("seen_urls", []))
        prs = [pr for pr in json.loads(out.stdout or "[]") if pr["url"] not in seen]  # --merged-at is day-granular
        self.ctx.log(f"{len(prs)} new merged PRs since {since[:10]}")
        signals = self.ctx.analyze(INSTRUCTIONS, "\n".join(summarize(pr) for pr in prs))
        if not self.ctx.dry_run:
            self.ctx.state["since"] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.ctx.state["seen_urls"] = (list(seen) + [pr["url"] for pr in prs])[-SEEN_KEEP:]
        return [s for s in signals if s["kind"] == SignalKind.DONE]
