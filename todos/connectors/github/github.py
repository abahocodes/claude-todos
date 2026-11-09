"""GitHub source: your merged pull requests since the last sync, as evidence signals.
The reconciler matches each PR title to an open todo; a match becomes a proposal to mark it done."""
import datetime as dt
import json
import subprocess

from todos.core.model import Authority, SignalKind


class Connector:
    def __init__(self, ctx):
        self.ctx = ctx

    def pull(self) -> list[dict]:
        since = self.ctx.state.get("since") or (dt.date.today() - dt.timedelta(days=3)).isoformat()
        out = subprocess.run(
            ["gh", "search", "prs", "--author", "@me", "--merged", f"--merged-at=>={since[:10]}",
             "--json", "title,url,closedAt", "--limit", "50"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            raise RuntimeError(f"gh search failed: {out.stderr.strip()[:200]}")
        self.ctx.state["since"] = dt.datetime.now(dt.timezone.utc).isoformat()
        return [{
            "kind": SignalKind.EVIDENCE, "authority": Authority.INFERRED, "confidence": 0.9,
            "title": pr["title"], "ref": pr["url"], "at": pr.get("closedAt"),
            "evidence": f"merged PR: {pr['title']}",
        } for pr in json.loads(out.stdout or "[]")]
