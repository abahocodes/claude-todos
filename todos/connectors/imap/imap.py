"""IMAP mailbox source. One instance per account; works with Gmail (app password), Outlook, iCloud, etc.

Strictly read-only: mailboxes are opened with EXAMINE (readonly=True) and bodies fetched with
BODY.PEEK, so nothing is marked read, moved, flagged or sent.

Settings (todo connector add imap --as <name> --set key=value):
  user (required), host (default imap.gmail.com), port (993), inbox (INBOX),
  sent (auto-detected via the \\Sent flag), max_messages (40), lookback_days (3, first run only)
"""
import datetime as dt
import email
import email.header
import email.utils
import imaplib
import re

INSTRUCTIONS = """These are recent emails from one of the user's mailboxes: IN = received, SENT = sent by the user.
- "new" only for a concrete ask directed at the user (a request, deadline, deliverable, form, payment,
  review, interview) that is not already on the list. Title it as an outcome ("Send W-9 to the accountant"),
  set "due" when a date is stated.
- "done" only when a SENT email clearly delivers what a listed open todo asks for.
- Messages marked [bulk] are usually newsletters or notifications: skip unless they carry a real
  personal deadline (an assessment invite, an interview reminder, a bill due).
- Put "imap:<message-id>" in "ref". At most 10 signals."""

TAGS = re.compile(r"<[^>]+>")
SPACES = re.compile(r"\s+")
QUOTE_START = re.compile(r"^(On .+wrote:|-----Original Message-----|From: .+)$", re.M)
SEEN_KEEP = 1000


def decode(v) -> str:
    if not v:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(v)))
    except Exception:
        return str(v)


def body_text(msg) -> str:
    plain, html = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = TAGS.sub(" ", text)
    text = plain if plain is not None else (html or "")
    m = QUOTE_START.search(text)  # drop the quoted thread below a reply
    if m:
        text = text[: m.start()]
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    return SPACES.sub(" ", text).strip()


def is_bulk(msg) -> bool:
    prec = (msg.get("Precedence") or "").lower()
    auto = (msg.get("Auto-Submitted") or "no").lower()
    return bool(msg.get("List-Unsubscribe")) or prec in ("bulk", "list", "junk") or auto != "no"


def summarize(raw: bytes, direction: str, snippet_chars: int = 600) -> tuple[str, str]:
    """Returns (message-id, one line for the model)."""
    msg = email.message_from_bytes(raw)
    mid = (msg.get("Message-ID") or "").strip()
    bulk = is_bulk(msg)
    who = decode(msg.get("To") if direction == "SENT" else msg.get("From"))
    date = msg.get("Date") or ""
    try:
        date = email.utils.parsedate_to_datetime(date).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    snippet = "" if bulk else body_text(msg)[:snippet_chars]
    line = (f"- {direction}{' [bulk]' if bulk else ''} {date} | {'To' if direction == 'SENT' else 'From'}: {who[:80]}"
            f" | Subject: {decode(msg.get('Subject'))[:150]} | id={mid}" + (f" | {snippet}" if snippet else ""))
    return mid, line


def quote_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


LIST_LINE = re.compile(rb'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)$')


def find_sent(conn) -> str | None:
    typ, rows = conn.list()
    if typ != "OK":
        return None
    for row in rows or []:
        m = LIST_LINE.match(row or b"")
        if m and b"\\Sent" in m.group("flags"):
            return m.group("name").decode().strip().strip('"')
    return None


class Connector:
    def __init__(self, ctx):
        self.ctx = ctx
        s = ctx.settings
        self.user = s.get("user")
        if not self.user:
            raise RuntimeError(f"set the account: todo config connectors.{ctx.name}.user you@example.com")
        self.password = ctx.secret()
        if not self.password:
            raise RuntimeError(f"no password: run `todo connector secret {ctx.name}` "
                               "(for Gmail, an app password from myaccount.google.com/apppasswords)")
        self.host = s.get("host", "imap.gmail.com")
        self.port = int(s.get("port", 993))
        self.max = int(s.get("max_messages", 40))

    def _since(self) -> dt.date:
        last = self.ctx.state.get("last_sync")
        if last:
            return dt.date.fromisoformat(last[:10]) - dt.timedelta(days=1)  # IMAP SINCE is day-granular
        return dt.date.today() - dt.timedelta(days=int(self.ctx.settings.get("lookback_days", 3)))

    def _fetch(self, conn, mailbox: str, direction: str, since: dt.date, seen: set) -> list[tuple[str, str]]:
        typ, _ = conn.select(quote_mailbox(mailbox), readonly=True)
        if typ != "OK":
            self.ctx.log(f"cannot open {mailbox}")
            return []
        typ, data = conn.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        ids = (data[0] or b"").split()[-self.max:] if typ == "OK" else []
        out = []
        for num in reversed(ids):  # newest first
            typ, parts = conn.fetch(num, "(BODY.PEEK[]<0.30000>)")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if typ != "OK" or raw is None:
                continue
            mid, line = summarize(raw, direction)
            if mid and mid in seen:
                continue
            out.append((mid, line))
        return out

    def pull(self) -> list[dict]:
        seen = set(self.ctx.state.get("seen_ids", []))
        since = self._since()
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self.password)
            items = self._fetch(conn, self.ctx.settings.get("inbox", "INBOX"), "IN", since, seen)
            sent_box = self.ctx.settings.get("sent") or find_sent(conn)
            if sent_box:
                items += self._fetch(conn, sent_box, "SENT", since, seen)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self.ctx.log(f"{len(items)} new messages since {since}")
        signals = self.ctx.analyze(INSTRUCTIONS, "\n".join(line for _, line in items))
        if not self.ctx.dry_run:
            self.ctx.state["seen_ids"] = (list(seen) + [mid for mid, _ in items if mid])[-SEEN_KEEP:]
        return signals
