"""Run: python3 -m unittest discover -s ~/.claude/skills/todos/tests -v"""
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from todos.core import paths, store, view  # noqa: E402
from todos.core.model import (Actor, Authority, Decision, Outcome, SignalKind, Status, actor,  # noqa: E402
                              validate_signal)
from todos.engine import queue, reconcile, transcript  # noqa: E402
from todos.engine.queue import JobKind, Reason  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TODOS_HOME": self.tmp.name})
        self.env.start()
        os.environ.pop("TODOS_WORKER", None)
        self.cfg = paths.load_config()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def sig(self, **kw):
        base = {"connector": "test", "confidence": 0.9, "authority": Authority.INFERRED}
        return validate_signal({**base, **kw})


class StoreTests(Base):
    def test_crud_and_history(self):
        with store.transaction() as tx:
            a = tx.create("Write report", due="2026-11-20")
            b = tx.create("Email Sam")
        with store.transaction(by=actor(Actor.CLAUDE)) as tx:
            tx.complete(b, evidence="sent it")
            tx.update(a, title="Write Q4 report")
        st = store.read()
        self.assertEqual(st["todos"][str(a)]["title"], "Write Q4 report")
        self.assertEqual(st["todos"][str(b)]["status"], Status.DONE)
        self.assertEqual(st["todos"][str(b)]["history"][-1]["evidence"], "sent it")
        self.assertEqual(view.build()["todos"], st["todos"])  # cache equals a fresh fold

    def test_ids_increase_and_drop_keeps_history(self):
        with store.transaction() as tx:
            a = tx.create("One")
            tx.drop(a, reason="not needed")
            b = tx.create("Two")
        self.assertEqual(b, a + 1)
        self.assertEqual(store.read()["todos"][str(a)]["status"], Status.DROPPED)

    def test_proposal_approve_and_reject(self):
        with store.transaction() as tx:
            a = tx.create("Ship v2")
            p1 = tx.propose("complete", self.sig(kind="done", todo=a), todo=a)
            p2 = tx.propose("create", self.sig(kind="new", title="Write changelog"))
        with store.transaction() as tx:
            tx.resolve(p1, Decision.APPROVED)
            tx.resolve(p2, Decision.REJECTED)
        st = store.read()
        self.assertEqual(st["todos"][str(a)]["status"], Status.DONE)
        self.assertEqual(st["proposals"], {})
        self.assertEqual(len(st["todos"]), 1)

    def test_torn_line_does_not_break_log(self):
        with store.transaction() as tx:
            tx.create("Survives")
        with open(paths.path("events.jsonl"), "a") as f:
            f.write('{"half": ')
        self.assertEqual(len(view.build()["todos"]), 1)


class SignalTests(Base):
    def test_validation(self):
        with self.assertRaises(ValueError):
            validate_signal({"kind": "explode"})
        with self.assertRaises(ValueError):
            validate_signal({"kind": "new"})
        with self.assertRaises(ValueError):
            validate_signal({"kind": "done"})
        s = validate_signal({"kind": "done", "todo": "#7", "confidence": 3})
        self.assertEqual((s["todo"], s["confidence"], s["authority"]), (7, 1.0, Authority.INFERRED))


class ReconcileTests(Base):
    def _todo(self, title="Send W-9 to the accountant"):
        with store.transaction() as tx:
            return tx.create(title)

    def test_inferred_done_becomes_proposal(self):
        tid = self._todo()
        [(_, outcome, _)] = reconcile.apply_signals([self.sig(kind="done", todo=tid)], self.cfg)
        self.assertEqual(outcome, Outcome.PROPOSED)
        self.assertEqual(store.read()["todos"][str(tid)]["status"], Status.OPEN)

    def test_auto_apply_threshold(self):
        tid = self._todo()
        self.cfg["auto_apply"] = True
        [(_, low, _)] = reconcile.apply_signals([self.sig(kind="done", todo=tid, confidence=0.5)], self.cfg)
        [(_, high, _)] = reconcile.apply_signals([self.sig(kind="done", todo=tid, confidence=0.95)], self.cfg)
        self.assertEqual((low, high), (Outcome.PROPOSED, Outcome.APPLIED))

    def test_user_authority_applies_and_drop_needs_user(self):
        tid = self._todo()
        [(_, o1, _)] = reconcile.apply_signals([self.sig(kind="drop", todo=tid, confidence=1.0)], self.cfg)
        self.assertEqual(o1, Outcome.PROPOSED)
        [(_, o2, _)] = reconcile.apply_signals(
            [self.sig(kind="drop", todo=tid, authority=Authority.USER)], self.cfg)
        self.assertEqual(o2, Outcome.APPLIED)

    def test_your_later_edit_wins(self):
        tid = self._todo()
        old = self.sig(kind="done", todo=tid, authority=Authority.EVIDENCE, at="2020-01-01T00:00:00+00:00")
        [(_, outcome, _)] = reconcile.apply_signals([old], self.cfg)
        self.assertEqual(outcome, Outcome.SUPERSEDED)

    def test_duplicates(self):
        self._todo("Send W-9 to the accountant")
        [(_, o1, _)] = reconcile.apply_signals([self.sig(kind="new", title="Send W9 to the accountant")], self.cfg)
        [(_, o2, _)] = reconcile.apply_signals([self.sig(kind="new", title="Renew domain")], self.cfg)
        [(_, o3, _)] = reconcile.apply_signals([self.sig(kind="new", title="Renew domain")], self.cfg)
        self.assertEqual((o1, o2, o3), (Outcome.DUPLICATE, Outcome.PROPOSED, Outcome.DUPLICATE))

    def test_reordered_title_is_duplicate(self):
        [(_, o1, _)] = reconcile.apply_signals(
            [self.sig(kind="new", title="Complete Acme Assessment on HackerRank")], self.cfg)
        [(_, o2, _)] = reconcile.apply_signals(
            [self.sig(kind="new", title="Complete Acme HackerRank assessment")], self.cfg)
        [(_, o3, _)] = reconcile.apply_signals(
            [self.sig(kind="new", title="Complete Globex interview prep")], self.cfg)
        self.assertEqual((o1, o2, o3), (Outcome.PROPOSED, Outcome.DUPLICATE, Outcome.PROPOSED))

    def test_evidence_matches_open_todo(self):
        tid = self._todo("Fix KVS chunking bug")
        [(_, o, t)] = reconcile.apply_signals(
            [self.sig(kind="evidence", title="Fix KVS chunking bug in exporter")], self.cfg)
        self.assertEqual((o, t), (Outcome.PROPOSED, tid))
        [(_, o2, _)] = reconcile.apply_signals([self.sig(kind="evidence", title="Bump lodash")], self.cfg)
        self.assertEqual(o2, Outcome.UNMATCHED)


class TranscriptTests(Base):
    def test_partial_line_not_consumed_and_tool_output_dropped(self):
        p = Path(self.tmp.name) / "t.jsonl"
        lines = [
            {"type": "user", "message": {"content": "please ship it <system-reminder>noise</system-reminder>"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Shipping."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "git push"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 5000}]}},
        ]
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n" + '{"type": "assistant", "mess')
        out, off, bad = transcript.read_delta(str(p), 0)
        self.assertEqual(out, ["USER: please ship it", "ASSISTANT: Shipping.", "ASSISTANT: [tool: Bash(git push)]"])
        self.assertLess(off, transcript.size(str(p)))  # stopped before the half-written line
        with open(p, "a") as f:
            f.write('age": {"content": "done"}}\n')
        out2, off2, _ = transcript.read_delta(str(p), off)
        self.assertEqual(out2, ["ASSISTANT: done"])
        self.assertEqual(off2, transcript.size(str(p)))

    def test_chunk_never_drops(self):
        lines = [f"line {i} " + "x" * 50 for i in range(100)]
        chunks = transcript.chunk(lines, 500)
        self.assertEqual("\n".join(chunks).count("line "), 100)


class QueueTests(Base):
    def test_coalesce(self):
        queue.enqueue(JobKind.SESSION, "abc", Reason.STOP)
        queue.enqueue(JobKind.SESSION, "abc", Reason.PRE_COMPACT)
        jobs = queue.take_all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["reasons"], [Reason.STOP, Reason.PRE_COMPACT])
        self.assertEqual(queue.take_all(), [])


FAKE_APP = textwrap.dedent('''
    import json, os
    from pathlib import Path
    DB = Path(os.environ["TODOS_HOME"]) / "fakeapp.json"
    def load():
        return json.loads(DB.read_text()) if DB.exists() else {"n": 0, "tasks": {}}
    def save(d):
        DB.write_text(json.dumps(d))
    class Connector:
        def __init__(self, ctx): pass
        def list_tasks(self):
            return [{"ext_id": k, **v} for k, v in load()["tasks"].items() if not v["done"]]
        def create(self, todo):
            d = load(); d["n"] += 1; k = f"x{d['n']}"
            d["tasks"][k] = {"title": todo["title"], "due": todo.get("due"), "done": False}; save(d); return k
        def update(self, k, f):
            d = load(); d["tasks"][k].update(title=f["title"], due=f.get("due")); save(d)
        def complete(self, k):
            d = load(); d["tasks"][k]["done"] = True; save(d)
        def reopen(self, k):
            d = load(); d["tasks"][k]["done"] = False; save(d)
        def delete(self, k):
            d = load(); d["tasks"].pop(k, None); save(d)
''')


class ReporterTests(Base):
    def setUp(self):
        super().setUp()
        d = Path(self.tmp.name) / "connectors" / "fakeapp"
        d.mkdir(parents=True)
        (d / "connector.json").write_text(json.dumps(
            {"name": "fakeapp", "type": "script", "roles": ["source", "reporter"], "module": "fakeapp.py"}))
        (d / "fakeapp.py").write_text(FAKE_APP)
        self.cfg["connectors"]["fakeapp"] = {"enabled": True}
        self.db = Path(self.tmp.name) / "fakeapp.json"

    def app(self):
        return json.loads(self.db.read_text())["tasks"]

    def edit_app(self, fn):
        d = json.loads(self.db.read_text())
        fn(d["tasks"])
        self.db.write_text(json.dumps(d))

    def test_two_way_sync(self):
        from todos.connectors import runner

        with store.transaction() as tx:
            a = tx.create("Book flights")
            b = tx.create("Renew passport", due="2026-12-01")
        runner.run_connector("fakeapp", self.cfg)
        self.assertEqual(sorted(t["title"] for t in self.app().values()), ["Book flights", "Renew passport"])

        # On the phone: complete one, rename the other, add a new one.
        def phone(tasks):
            for k, t in tasks.items():
                if t["title"] == "Book flights":
                    t["done"] = True
                else:
                    t["title"] = "Renew passport (photos first)"
            tasks["p1"] = {"title": "Call plumber", "due": None, "done": False}
        self.edit_app(phone)
        runner.run_connector("fakeapp", self.cfg)
        st = store.read()
        self.assertEqual(st["todos"][str(a)]["status"], Status.DONE)
        self.assertEqual(st["todos"][str(b)]["title"], "Renew passport (photos first)")
        self.assertIn("Call plumber", [t["title"] for t in st["todos"].values()])
        self.assertEqual(len(self.app()), 3)  # no duplicate pushed for the phone-added task

        # Locally: drop one, it disappears from the app.
        with store.transaction() as tx:
            tx.drop(b)
        runner.run_connector("fakeapp", self.cfg)
        self.assertNotIn("Renew passport (photos first)", [t["title"] for t in self.app().values()])

        # Remove the connector: history kept, map archived.
        runner.remove_reporter_map("fakeapp")
        self.assertEqual(len(store.read()["todos"]), 3)


ICS_SAMPLE = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
UID:abc123\r
DTSTART;TZID=America/Los_Angeles:{start}\r
SUMMARY:Globex coding\\, design interview\r
DESCRIPTION:Bring a laptop.\\nZoom link inside\r
 and more folded text\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:old\r
DTSTART:20200101T100000Z\r
SUMMARY:Ancient\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancelled\r
DTSTART:{start_utc}\r
STATUS:CANCELLED\r
SUMMARY:Called off\r
END:VEVENT\r
END:VCALENDAR\r
"""

MAIL = b"""Message-ID: <m1@example.com>\r
From: Recruiter <r@globex.example>\r
To: me@gmail.com\r
Subject: =?utf-8?q?Interview_=E2=9C=93?=\r
Date: Sun, 08 Nov 2026 10:00:00 -0800\r
Content-Type: text/plain; charset=utf-8\r
\r
Please confirm your slot by Tuesday.\r
\r
On Sat, Nov 7, 2026 Someone wrote:\r
> old quoted text\r
"""


class MultiConnectorTests(Base):
    def test_instances_resolve_with_own_settings(self):
        from todos.connectors.base import enabled, resolve

        self.cfg["connectors"]["personal-mail"] = {"enabled": True, "template": "imap", "user": "me@gmail.com"}
        self.cfg["connectors"]["work-cal"] = {"enabled": False, "template": "ics"}
        self.cfg["connectors"]["bogus"] = {"enabled": True, "template": "sessions"}  # not a multi template
        m = resolve("personal-mail", self.cfg)
        self.assertEqual((m["name"], m["template"]), ("personal-mail", "imap"))
        self.assertIsNone(resolve("imap", self.cfg))  # a template is never run directly
        self.assertIsNone(resolve("bogus", self.cfg))
        self.assertEqual(sorted(enabled(self.cfg)), ["personal-mail", "sessions"])

    def test_secret_from_1password_ref_and_locked_backoff(self):
        from todos.connectors import base, runner

        self.cfg["connectors"]["personal-mail"] = {"enabled": True, "template": "imap",
                                                   "user": "me@gmail.com", "secret_ref": "op://Private/Mail/pw"}
        ctx = base.Ctx(base.resolve("personal-mail", self.cfg), self.cfg)
        ok = mock.Mock(returncode=0, stdout="s3cret", stderr="")
        with mock.patch("todos.connectors.base.keychain_read", return_value=None), \
                mock.patch("todos.connectors.base.subprocess.run", return_value=ok) as run:
            self.assertEqual(ctx.secret(), "s3cret")
            self.assertEqual(run.call_args[0][0], ["op", "read", "--no-newline", "op://Private/Mail/pw"])
        locked = mock.Mock(returncode=1, stdout="", stderr="account is not signed in")
        with mock.patch("todos.connectors.base.keychain_read", return_value=None), \
                mock.patch("todos.connectors.base.subprocess.run", return_value=locked):
            with self.assertRaises(base.SecretUnavailable):
                runner.run_connector("personal-mail", self.cfg)
        self.assertNotIn("personal-mail", runner.due_connectors(self.cfg))  # backing off for an hour

    def test_googletasks_reporter_calls(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gt_t", Path(__file__).resolve().parents[1] / "todos/connectors/googletasks/googletasks.py")
        gt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gt)
        from todos.connectors import base

        ctx = base.Ctx(base.resolve("googletasks", self.cfg), self.cfg)
        calls = []

        def fake(method, path, body=None, query=None):
            calls.append((method, path, body))
            if path == "/users/@me/lists":
                return {"items": [{"id": "L1", "title": "Groceries"}]} if method == "GET" else {"id": "L2"}
            if method == "GET":
                return {"items": [{"id": "t1", "title": "Call plumber", "due": "2026-11-12T00:00:00.000Z",
                                   "status": "needsAction"}, {"id": "t2", "title": "Old", "status": "completed"}]}
            return {"id": "t9"}

        with mock.patch.object(base.Ctx, "secret", return_value='{"client_id":"a","client_secret":"b","refresh_token":"c"}'), \
                mock.patch.object(gt, "access_token", return_value="tok"):
            c = gt.Connector(ctx)
        c._req = fake
        self.assertEqual(c.list_tasks(), [{"ext_id": "t1", "title": "Call plumber", "due": "2026-11-12"}])
        self.assertEqual(calls[1], ("POST", "/users/@me/lists", {"title": "Claude Todos"}))  # never reuses "Groceries"
        self.assertEqual(c.create({"id": 3, "title": "Ship", "due": "2026-11-20"}), "t9")
        self.assertEqual(calls[-1][2]["due"], "2026-11-20T00:00:00.000Z")
        c.reopen("t1")
        self.assertEqual(calls[-1][2], {"status": "needsAction", "completed": None})

    def test_oauth_client_file(self):
        from todos.connectors import oauth

        p = Path(self.tmp.name) / "client.json"
        p.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "sec"}}))
        self.assertEqual(oauth.load_client(str(p))["client_id"], "id")
        p.write_text(json.dumps({"installed": {"client_id": "id"}}))
        with self.assertRaises(oauth.OAuthError):
            oauth.load_client(str(p))

    def test_cli_add_requires_instance_name(self):
        from todos.cli import main

        with self.assertRaises(SystemExit):
            main(["connector", "add", "imap"])

    def test_ics_parse_window_and_unfold(self):
        import datetime as dt
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ics_t", Path(__file__).resolve().parents[1] / "todos/connectors/ics/ics.py")
        ics = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ics)
        start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        text = ICS_SAMPLE.format(start=start.astimezone().strftime("%Y%m%dT%H%M%S"),
                                 start_utc=start.strftime("%Y%m%dT%H%M%SZ"))
        events = ics.in_window(ics.parse_events(text), dt.datetime.now(dt.timezone.utc),
                               dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7))
        self.assertEqual([e["uid"] for e in events], ["abc123"])
        self.assertEqual(events[0]["summary"], "Globex coding, design interview")
        self.assertIn("and more folded text", events[0]["description"])

    def test_imap_summary_strips_quotes_and_decodes(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "imap_t", Path(__file__).resolve().parents[1] / "todos/connectors/imap/imap.py")
        imap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(imap)
        mid, line = imap.summarize(MAIL, "IN")
        self.assertEqual(mid, "<m1@example.com>")
        self.assertIn("Subject: Interview ✓", line)
        self.assertIn("Please confirm your slot by Tuesday.", line)
        self.assertNotIn("old quoted text", line)
        self.assertNotIn("[bulk]", line)
        bulk = MAIL.replace(b"Subject:", b"List-Unsubscribe: <mailto:x@y>\r\nSubject:")
        _, bline = imap.summarize(bulk, "IN")
        self.assertIn("[bulk]", bline)
        self.assertNotIn("Please confirm", bline)  # bulk mail sends subject only
        self.assertEqual(imap.find_sent(type("C", (), {"list": lambda s: ("OK", [
            b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"'])})()),
            "[Gmail]/Sent Mail")

    def test_github_sends_unseen_prs_to_model_and_keeps_only_done(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "github_t", Path(__file__).resolve().parents[1] / "todos/connectors/github/github.py")
        gh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gh)
        prs = [{"title": "fix(auth): route email sign-in to check-email page", "url": "u/1", "body": "Magic\nlink",
                "repository": {"nameWithOwner": "me/app"}},
               {"title": "chore: bump deps", "url": "u/0", "body": "", "repository": {"nameWithOwner": "me/app"}}]
        got = {}

        def analyze(instructions, material):
            got["material"] = material
            return [{"kind": SignalKind.DONE, "todo": 3}, {"kind": SignalKind.NEW, "title": "x"}]

        ctx = type("Ctx", (), {"state": {"seen_urls": ["u/0"]}, "dry_run": False, "log": lambda s, m: None,
                               "analyze": lambda s, i, m: analyze(i, m)})()
        run = mock.Mock(returncode=0, stdout=json.dumps(prs))
        with mock.patch.object(gh.subprocess, "run", return_value=run):
            signals = gh.Connector(ctx).pull()
        self.assertEqual([s["kind"] for s in signals], [SignalKind.DONE])
        self.assertIn("[me/app] fix(auth): route email sign-in to check-email page (u/1)\n  Magic link", got["material"])
        self.assertNotIn("bump deps", got["material"])
        self.assertEqual(sorted(ctx.state["seen_urls"]), ["u/0", "u/1"])


def _ics_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ics_r", Path(__file__).resolve().parents[1] / "todos/connectors/ics/ics.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class RecurrenceTests(Base):
    def setUp(self):
        super().setUp()
        import datetime as dt
        from zoneinfo import ZoneInfo

        self.dt, self.tz = dt, ZoneInfo("America/Los_Angeles")
        self.ics = _ics_module()

    def ev(self, start, rrule=None, **kw):
        return {"uid": "u1", "summary": "Standup", "dtstart": start, "dtend": start + self.dt.timedelta(minutes=15),
                "rrule": rrule, "exdates": [], **kw}

    def test_weekly_byday_with_exdate_and_moved_instance(self):
        d, tz = self.dt, self.tz
        first = d.datetime(2026, 11, 2, 9, 0, tzinfo=tz)  # a Monday
        base = self.ev(first, "FREQ=WEEKLY;BYDAY=MO,WE,FR",
                       exdates=[d.datetime(2026, 11, 11, 9, 0, tzinfo=tz)])
        moved = {**self.ev(d.datetime(2026, 11, 13, 14, 0, tzinfo=tz)),
                 "recurrence_id": d.datetime(2026, 11, 13, 9, 0, tzinfo=tz), "rrule": None}
        got = self.ics.expand([base, moved], d.datetime(2026, 11, 9, tzinfo=tz), d.datetime(2026, 11, 16, tzinfo=tz))
        self.assertEqual([e["dtstart"].strftime("%a %H:%M") for e in got], ["Mon 09:00", "Fri 14:00"])

    def test_count_until_and_dst(self):
        d, tz = self.dt, self.tz
        first = d.datetime(2026, 10, 30, 10, 0, tzinfo=tz)  # DST ends Nov 1
        got = self.ics.expand([self.ev(first, "FREQ=DAILY;COUNT=5")], first, first + d.timedelta(days=30))
        self.assertEqual(len(got), 5)
        self.assertTrue(all(e["dtstart"].hour == 10 for e in got))  # same wall time across the DST change
        got = self.ics.expand([self.ev(first, "FREQ=WEEKLY;UNTIL=20261110T000000Z")], first, first + d.timedelta(days=60))
        self.assertEqual(len(got), 2)

    def test_old_daily_series_reaches_today(self):
        d, tz = self.dt, self.tz
        first = d.datetime(2009, 1, 1, 8, 0, tzinfo=tz)
        start = d.datetime(2026, 11, 9, tzinfo=tz)
        got = self.ics.expand([self.ev(first, "FREQ=DAILY")], start, start + d.timedelta(days=2))
        self.assertEqual(len(got), 2)


class AlertTests(Base):
    def test_soon_now_once_per_session_and_due_today(self):
        import datetime as dt
        from todos.core import agenda

        now = dt.datetime.now(dt.timezone.utc)
        agenda.save("cal", [
            {"start": (now + dt.timedelta(minutes=20)).isoformat(), "title": "Globex interview"},
            {"start": (now + dt.timedelta(hours=3)).isoformat(), "title": "Later thing"},
            {"start": (now - dt.timedelta(hours=2)).isoformat(), "end": (now - dt.timedelta(hours=1)).isoformat(), "title": "Done already"},
        ])
        with store.transaction() as tx:
            tx.create("Finish HackerRank", due=now.astimezone().date().isoformat())
        st = store.read()
        lines, keys = agenda.alerts(self.cfg, st, set(), now)
        self.assertEqual(len(lines), 2)
        self.assertIn("Globex interview starts in 20 min", lines[0])
        self.assertIn("#1 Finish HackerRank", lines[1])
        again, _ = agenda.alerts(self.cfg, st, keys, now)
        self.assertEqual(again, [])  # nothing repeats in the same session
        later, _ = agenda.alerts(self.cfg, st, keys, now + dt.timedelta(minutes=17))
        self.assertEqual(len(later), 1)
        self.assertIn("starts in 3 min", later[0])  # the 5-minute alert is separate

    def test_prompt_hook_surfaces_alert(self):
        import datetime as dt
        from todos import hooks
        from todos.core import agenda

        agenda.save("cal", [{"start": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat(),
                             "title": "Initech interview"}])
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "a1"}))), redirect_stdout(buf):
            hooks.run("prompt")
        ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TIME-SENSITIVE", ctx)
        self.assertIn("Initech interview starts in 10 min", ctx)


class HookTests(Base):
    def run_hook(self, event, payload):
        from todos import hooks

        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch("todos.hooks.ensure_worker") as ew, redirect_stdout(buf):
            rc = hooks.run(event)
        return rc, buf.getvalue(), ew

    def test_session_start_injects_context(self):
        with store.transaction() as tx:
            tx.create("Finish tax forms")
        rc, out, _ = self.run_hook("session-start", {"session_id": "s1", "transcript_path": "/nope"})
        self.assertEqual(rc, 0)
        ctx = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("#1 Finish tax forms", ctx["additionalContext"])

    def test_stop_is_throttled(self):
        p = Path(self.tmp.name) / "s.jsonl"
        p.write_text("")
        self.run_hook("session-start", {"session_id": "s2", "transcript_path": str(p)})
        p.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        _, _, ew = self.run_hook("stop", {"session_id": "s2", "transcript_path": str(p)})
        ew.assert_not_called()  # under min_chars
        p.write_text((json.dumps({"type": "user", "message": {"content": "x" * 3000}}) + "\n"))
        _, _, ew = self.run_hook("stop", {"session_id": "s2", "transcript_path": str(p)})
        ew.assert_called_once()

    def test_errors_never_escape_and_recursion_guard(self):
        rc, out, _ = self.run_hook("not-an-event", {})
        self.assertEqual((rc, out), (0, ""))
        with mock.patch.dict(os.environ, {"TODOS_WORKER": "1"}):
            rc, out, _ = self.run_hook("session-start", {"session_id": "s3"})
        self.assertEqual((rc, out), (0, ""))

    def test_prompt_reports_outside_changes_once(self):
        with store.transaction() as tx:
            tid = tx.create("Pay invoice")
        self.run_hook("session-start", {"session_id": "s4"})
        with store.transaction(by=actor(Actor.PHONE, "todoist")) as tx:
            tx.complete(tid)
        _, out, _ = self.run_hook("prompt", {"session_id": "s4"})
        self.assertIn("Pay invoice: done (phone:todoist)", out)
        _, out2, _ = self.run_hook("prompt", {"session_id": "s4"})
        self.assertEqual(out2, "")


class CliTests(Base):
    def cli(self, *argv):
        from todos.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(list(argv))
        return rc, buf.getvalue()

    def test_add_list_done_config(self):
        self.cli("add", "Draft blog post", "--due", "2026-11-30")
        self.cli("done", "1", "--by", "claude", "--evidence", "published")
        _, out = self.cli("list", "--all")
        self.assertIn("<done>", out)
        self.cli("config", "throttle.min_chars", "5000")
        self.assertEqual(paths.load_config()["throttle"]["min_chars"], 5000)
        saved = json.loads(paths.path("config.json").read_text())
        self.assertEqual(saved, {"throttle": {"min_chars": 5000}})  # only differences are saved


if __name__ == "__main__":
    unittest.main()
