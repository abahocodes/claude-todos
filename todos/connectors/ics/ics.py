"""iCal feed source. One instance per calendar: the secret is the feed's private URL.

pull():    events in the next `days_ahead` days (default 7) -> Haiku (ctx.analyze) -> signals.
agenda():  the same events with exact times, no AI; the sweeper refreshes it every 15 minutes
           so in-session alerts know what's coming up.
Recurring events are expanded (DAILY / WEEKLY with BYDAY / MONTHLY / YEARLY, INTERVAL, COUNT,
UNTIL, EXDATE, and moved single instances via RECURRENCE-ID). Exotic rules (BYSETPOS, numbered
BYDAY like 2TU) fall back to the plain weekday."""
import datetime as dt
import re
import urllib.request

INSTRUCTIONS = """These are upcoming events from one of the user's calendars.
- "new" for a deadline, or a meeting that clearly needs preparation (interview, demo, presentation,
  review, submission), that is not already on the list. Set "due" to the event date (prep: the day before).
- "update" with fields.due when an event clearly sets or moves the due date of a listed todo.
- Ignore routine meetings, holidays, focus blocks, and events with no action for the user.
- Put "ics:<uid>" in "ref". At most 8 signals."""

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_STEPS = 5000


def unfold(text: str) -> list[str]:
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_dt(value: str, params: str) -> dt.datetime | None:
    v = value.strip()
    try:
        if re.fullmatch(r"\d{8}", v):  # all-day: midnight local
            return dt.datetime.strptime(v, "%Y%m%d").astimezone()
        if v.endswith("Z"):
            return dt.datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        naive = dt.datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
        tz = re.search(r"TZID=([^;:]+)", params or "")
        if tz:
            from zoneinfo import ZoneInfo
            try:
                return naive.replace(tzinfo=ZoneInfo(tz.group(1).strip('"')))
            except Exception:
                pass
        return naive.astimezone()  # floating time: local
    except ValueError:
        return None


def parse_events(text: str) -> list[dict]:
    events, cur = [], None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": []}
        elif line == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                cur[name.lower().replace("-", "_")] = parse_dt(value, params)
                if name == "DTSTART":
                    cur["all_day"] = "VALUE=DATE" in params or bool(re.fullmatch(r"\d{8}", value.strip()))
            elif name == "EXDATE":
                cur["exdates"] += [d for d in (parse_dt(v, params) for v in value.split(",")) if d]
            elif name in ("SUMMARY", "DESCRIPTION", "LOCATION", "UID", "STATUS"):
                cur[name.lower()] = _unescape(value)
            elif name == "RRULE":
                cur["rrule"] = value.strip()
    return events


def _rule(s: str) -> dict:
    return dict(p.split("=", 1) for p in s.split(";") if "=" in p)


def _add_months(t: dt.datetime, n: int) -> dt.datetime | None:
    m = t.month - 1 + n
    try:
        return t.replace(year=t.year + m // 12, month=m % 12 + 1)
    except ValueError:  # e.g. the 31st in a short month: RFC 5545 skips it
        return None


def occurrences(e: dict, start: dt.datetime, end: dt.datetime):
    """Yield start times of e's instances that fall in [start, end)."""
    first = e.get("dtstart")
    if not first:
        return
    if not e.get("rrule"):
        if start <= first < end:
            yield first
        return
    r = _rule(e["rrule"])
    freq, step = r.get("FREQ"), int(r.get("INTERVAL", 1) or 1)
    count = int(r["COUNT"]) if r.get("COUNT") else None
    until = parse_dt(r["UNTIL"], "") if r.get("UNTIL") else None
    days = sorted({WEEKDAYS[d[-2:]] for d in r.get("BYDAY", "").split(",") if d[-2:] in WEEKDAYS})
    excluded = {x.astimezone(dt.timezone.utc).replace(second=0) for x in e.get("exdates", [])}
    n, i0 = 0, 0
    if count is None and start > first:  # no COUNT to honour: jump close to the window instead of walking from 2009
        span_days = (start - first).days
        period_days = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 28, "YEARLY": 365}.get(freq, 1) * step
        i0 = max(0, span_days // period_days - 2)
    for i in range(i0, i0 + MAX_STEPS):
        if freq == "DAILY":
            batch = [first + dt.timedelta(days=i * step)]
        elif freq == "WEEKLY":
            week0 = first - dt.timedelta(days=first.weekday())
            wk = week0 + dt.timedelta(weeks=i * step)
            batch = [wk + dt.timedelta(days=d) for d in (days or [first.weekday()])]
        elif freq == "MONTHLY":
            batch = [x for x in [_add_months(first, i * step)] if x]
        elif freq == "YEARLY":
            batch = [x for x in [_add_months(first, 12 * i * step)] if x]
        else:
            return
        for t in batch:
            if t < first:
                continue
            if (until and t > until) or (count is not None and n >= count) or t >= end:
                return
            n += 1
            if t >= start and t.astimezone(dt.timezone.utc).replace(second=0) not in excluded:
                yield t


def expand(events: list[dict], start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Instances in the window, with moved single instances replacing the original slot."""
    moved = {(e.get("uid"), e["recurrence_id"].astimezone(dt.timezone.utc))
             for e in events if e.get("recurrence_id")}
    out = []
    for e in events:
        if e.get("status", "").upper() == "CANCELLED":
            continue
        length = (e["dtend"] - e["dtstart"]) if e.get("dtend") and e.get("dtstart") else None
        for t in occurrences(e, start, end):
            if not e.get("recurrence_id") and (e.get("uid"), t.astimezone(dt.timezone.utc)) in moved:
                continue
            out.append({**e, "dtstart": t, "dtend": t + length if length else None})
    return sorted(out, key=lambda x: x["dtstart"])


def in_window(events: list[dict], start: dt.datetime, end: dt.datetime) -> list[dict]:
    return expand(events, start, end)


def render(events: list[dict]) -> str:
    lines = []
    for e in events:
        when = e["dtstart"].astimezone().strftime("%a %Y-%m-%d") + ("" if e.get("all_day") else
                                                                   e["dtstart"].astimezone().strftime(" %H:%M"))
        desc = (e.get("description") or "")[:300]
        lines.append(f"- {when} | {e.get('summary', '(no title)')} | uid={e.get('uid', '?')}"
                     + (f" | {desc}" if desc else ""))
    return "\n".join(lines)


class Connector:
    def __init__(self, ctx):
        self.ctx = ctx
        self.url = ctx.secret()
        if not self.url:
            raise RuntimeError(f"no feed URL: run `todo connector secret {ctx.name}` and paste the private iCal address")

    def fetch(self) -> str:
        url = self.url.replace("webcal://", "https://", 1)
        req = urllib.request.Request(url, headers={"User-Agent": "todos-ics/0.2"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    def window(self, days: int) -> list[dict]:
        now = dt.datetime.now(dt.timezone.utc)
        return in_window(parse_events(self.fetch()), now - dt.timedelta(hours=12), now + dt.timedelta(days=days))

    def agenda(self) -> list[dict]:
        """Next 7 days with exact times, for alerts. No AI."""
        return [{"start": e["dtstart"].isoformat(), "end": e["dtend"].isoformat() if e.get("dtend") else None,
                 "title": e.get("summary", "(no title)"), "all_day": e.get("all_day", False)}
                for e in self.window(7)]

    def pull(self) -> list[dict]:
        days = int(self.ctx.settings.get("days_ahead", 7))
        now = dt.datetime.now(dt.timezone.utc)
        events = [e for e in self.window(days) if e["dtstart"] >= now]
        self.ctx.log(f"{len(events)} events in the next {days} days")
        return self.ctx.analyze(INSTRUCTIONS, render(events))
