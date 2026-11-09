"""Todoist reporter (and source, via the shared sync). Uses the Todoist API v1 with a personal token.

Only the five reporter calls live here; runner.sync_reporter does all the diffing.
Tasks are kept in one project (setting "project", default "Claude Todos") so nothing else is touched.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.todoist.com/api/v1"


class Connector:
    def __init__(self, ctx):
        self.ctx = ctx
        self.token = ctx.secret()
        if not self.token:
            raise RuntimeError("no Todoist token: set TODOIST_TOKEN or run "
                               "`security add-generic-password -s todos-todoist -a todos -w <token>`")
        self.project_name = ctx.settings.get("project", "Claude Todos")
        self._project_id = ctx.state.get("project_id")

    # HTTP --------------------------------------------------------------------
    def _req(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Todoist {method} {path}: HTTP {e.code} {e.read()[:200]!r}") from e
        return json.loads(raw) if raw.strip() else None

    def _paged(self, path: str, query: dict) -> list:
        out, cursor = [], None
        while True:
            q = dict(query, **({"cursor": cursor} if cursor else {}))
            page = self._req("GET", path, query=q)
            if isinstance(page, list):  # tolerate a non-paginated response
                return out + page
            out += page.get("results", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return out

    def project_id(self) -> str:
        if self._project_id:
            return self._project_id
        for p in self._paged("/projects", {}):
            if p.get("name") == self.project_name:
                self._project_id = str(p["id"])
                break
        else:
            self._project_id = str(self._req("POST", "/projects", {"name": self.project_name})["id"])
        self.ctx.state["project_id"] = self._project_id
        return self._project_id

    # Reporter contract ------------------------------------------------------------
    def list_tasks(self) -> list[dict]:
        tasks = self._paged("/tasks", {"project_id": self.project_id()})
        return [{"ext_id": str(t["id"]), "title": t.get("content", ""),
                 "due": ((t.get("due") or {}).get("date") or "")[:10] or None}
                for t in tasks if not t.get("checked") and not t.get("is_deleted")]

    def create(self, todo: dict) -> str:
        body = {"content": todo["title"], "project_id": self.project_id(),
                "description": f"todos #{todo['id']}" + (f"\n{todo['notes']}" if todo.get("notes") else "")}
        if todo.get("due"):
            body["due_date"] = str(todo["due"])[:10]
        return str(self._req("POST", "/tasks", body)["id"])

    def update(self, ext_id: str, fields: dict) -> None:
        body = {"content": fields["title"]}
        body.update({"due_date": str(fields["due"])[:10]} if fields.get("due") else {"due_string": "no date"})
        self._req("POST", f"/tasks/{ext_id}", body)

    def complete(self, ext_id: str) -> None:
        self._req("POST", f"/tasks/{ext_id}/close")

    def reopen(self, ext_id: str) -> None:
        self._req("POST", f"/tasks/{ext_id}/reopen")

    def delete(self, ext_id: str) -> None:
        self._req("DELETE", f"/tasks/{ext_id}")
