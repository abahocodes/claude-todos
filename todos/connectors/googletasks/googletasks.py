"""Google Tasks reporter (and source, via the shared sync). Google Tasks API v1 with an OAuth grant.

Only the reporter calls live here; runner.sync_reporter does the diffing. Everything stays in one
task list (setting "list", default "Claude Todos"), so your other lists are never touched.
Google Tasks stores dates only (no times), which matches the todo `due` field.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from todos.connectors.oauth import access_token

API = "https://tasks.googleapis.com/tasks/v1"


def to_due(d) -> str | None:
    return f"{str(d)[:10]}T00:00:00.000Z" if d else None


class Connector:
    def __init__(self, ctx):
        self.ctx = ctx
        raw = ctx.secret()
        if not raw:
            raise RuntimeError(f"not signed in: run `todo connector auth {ctx.name} --client <oauth-client.json>`")
        self.token = access_token(json.loads(raw))
        self.list_name = ctx.settings.get("list", "Claude Todos")
        self._list_id = ctx.state.get("list_id")

    def _req(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Google Tasks {method} {path}: HTTP {e.code} {e.read()[:200]!r}") from e
        return json.loads(raw) if raw.strip() else None

    def _paged(self, path: str, query: dict) -> list:
        out, token = [], None
        while True:
            page = self._req("GET", path, query=dict(query, **({"pageToken": token} if token else {})))
            out += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return out

    def list_id(self) -> str:
        if self._list_id:
            return self._list_id
        for tl in self._paged("/users/@me/lists", {"maxResults": 100}):
            if tl.get("title") == self.list_name:
                self._list_id = tl["id"]
                break
        else:
            self._list_id = self._req("POST", "/users/@me/lists", {"title": self.list_name})["id"]
        self.ctx.state["list_id"] = self._list_id
        return self._list_id

    # Reporter contract ------------------------------------------------------------
    def list_tasks(self) -> list[dict]:
        items = self._paged(f"/lists/{self.list_id()}/tasks",
                            {"showCompleted": "false", "showHidden": "false", "showDeleted": "false", "maxResults": 100})
        return [{"ext_id": t["id"], "title": t.get("title", ""), "due": (t.get("due") or "")[:10] or None}
                for t in items if t.get("status") != "completed" and t.get("title")]

    def create(self, todo: dict) -> str:
        body = {"title": todo["title"], "notes": f"todos #{todo['id']}" + (f"\n{todo['notes']}" if todo.get("notes") else "")}
        if todo.get("due"):
            body["due"] = to_due(todo["due"])
        return self._req("POST", f"/lists/{self.list_id()}/tasks", body)["id"]

    def update(self, ext_id: str, fields: dict) -> None:
        self._req("PATCH", f"/lists/{self.list_id()}/tasks/{ext_id}",
                  {"title": fields["title"], "due": to_due(fields.get("due"))})

    def complete(self, ext_id: str) -> None:
        self._req("PATCH", f"/lists/{self.list_id()}/tasks/{ext_id}", {"status": "completed"})

    def reopen(self, ext_id: str) -> None:
        self._req("PATCH", f"/lists/{self.list_id()}/tasks/{ext_id}", {"status": "needsAction", "completed": None})

    def delete(self, ext_id: str) -> None:
        self._req("DELETE", f"/lists/{self.list_id()}/tasks/{ext_id}")
