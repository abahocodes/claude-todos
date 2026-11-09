# claude-todos

A Claude Code plugin that gives you **one todo list shared by every Claude session**. You don't have to tell it when something is done or new: it notices as you work, reads signals from your email and calendar, alerts you before meetings, and mirrors everything to your phone.

```
> /todos what should I do next
Tonight: prep for Globex (#1) at 9am tomorrow.
Monday: finish the Acme assessment (#2), due Tue 7:15pm.

> (later, mid-session)
⏰ Initech interview starts in 25 min (3:30pm).
```

## What it does

- **One list, every session.** Each session starts knowing your open todos, pending proposals and the rest of today's calendar.
- **Keeps itself current.** Claude marks todos done as it finishes them. A throttled background check (Haiku) reads only the new part of each session transcript and catches what Claude missed, including in sessions that stay open all day.
- **Talk to it.** `/todos` adds, edits, completes and drops todos, and answers "what's stale?", "what should I rethink?", "break this down".
- **Meeting alerts.** When you send a message and an event starts within 30 minutes (and again at 5), Claude tells you first.
- **Modular connectors.** Sources send signals in; reporters mirror the list to an app and send your edits back. Add or remove any of them without losing history.
- **Safe by default.** Inferred changes arrive as proposals you approve. Your own edits always win. Nothing is ever deleted automatically. Email and calendar access is read-only.

## Connectors

| Connector | Role | How it connects |
|---|---|---|
| `sessions` | source | built in: your Claude Code transcripts |
| `gmail`, `calendar` | source | your claude.ai Gmail / Google Calendar connectors (read-only) |
| `imap` (template) | source | any mailbox, one instance per account, opened read-only |
| `ics` (template) | source | any calendar's private iCal link, one instance per calendar; also feeds alerts |
| `github` | source | merged PRs via the `gh` CLI count as evidence |
| `googletasks` | source + reporter | two-way sync with a "Claude Todos" list in Google Tasks |
| `todoist` | source + reporter | two-way sync with a Todoist project |

## Install

Requirements: **macOS** (uses the Keychain and launchd), Python 3.11+, Claude Code.

```
/plugin marketplace add abahocodes/claude-todos
/plugin install todos@claude-todos
```

Then, in a terminal:

```bash
alias todo="python3 <plugin dir>/bin/todo"   # path shown by: claude plugin list
todo doctor                                  # health check
todo launchd install                         # 15-minute sweeper (idle sessions, overdue syncs, agenda refresh)
```

After updating the plugin, run `todo launchd install` again so the sweeper points at the new version.

## Adding connectors

Secrets go in the macOS Keychain (or 1Password via an `op://` reference in the connector's `secret_ref` setting). They never go in the repo or the chat.

```bash
# claude.ai connectors you already have
todo connector add gmail
todo connector add calendar

# another mailbox (Gmail needs an app password: myaccount.google.com/apppasswords)
todo connector secret work-mail
todo connector add imap --as work-mail --set user=you@example.com

# another calendar (Google Calendar: Settings > your calendar > "Secret address in iCal format")
todo connector secret work-cal
todo connector add ics --as work-cal

# your phone, via Google Tasks (needs your own OAuth client, see below)
todo connector auth googletasks --client ~/Downloads/client_secret.json --login-hint you@gmail.com
todo connector add googletasks
```

**Google Tasks OAuth client:** in a Google Cloud project, enable the Tasks API, configure the Google Auth Platform (External audience, then publish to production so sign-in doesn't expire after 7 days), and create an OAuth client of type *Desktop app*. Download its JSON and pass it to `todo connector auth`. Google shows an "unverified app" notice because it's your own app; that's expected.

## How it works

- **Master copy:** `~/.claude/todos/events.jsonl` is an append-only event log. `todos.json` is the current list, rebuilt from it. Every write goes through `bin/todo` under one file lock, so parallel sessions are safe.
- **Signals:** every source emits the same signal JSON (`new`, `done`, `update`, `drop`, `reopen`, `evidence`, plus confidence and authority). The reconciler is the only part that decides what happens to a signal:
  1. your own edits (phone app, `/todos`) always apply
  2. hard evidence (a merged PR) applies unless you edited the todo afterwards
  3. inferred signals (transcripts, email wording) become proposals unless `auto_apply` is on and confidence is high
- **Background checks** run after a reply (only past 2,000 new characters and 30 minutes since the last check), at PreCompact and SessionEnd, and from the sweeper for sessions idle 10+ minutes. Transcripts are read incrementally, tool output is dropped, and nothing is truncated: long stretches are chunked.
- **Cost:** checks use Haiku through headless `claude -p`. With a Claude subscription they count toward your usage limits; with an API key they're billed. `todo stats` shows the API-equivalent amounts.

## Layout

```
bin/todo               single entry point: CLI, hooks, worker, sweeper
todos/core/            paths, lock, model (enums), events, view, store, state, rules, render, agenda
todos/engine/          queue, spawn, transcript, llm, reconcile, sweep
todos/connectors/      base (contract, registry, secrets), runner (shared sync), oauth, and one folder per connector
todos/hooks.py         SessionStart, UserPromptSubmit, Stop, PreCompact, SessionEnd
SKILL.md               the /todos skill
tests/                 python3 -m unittest discover -s tests
```

## Write your own connector

Create `~/.claude/todos/connectors/<name>/connector.json` with `roles` (`source`, `reporter` or both) and `type` (`script` or `agent`), then add:

- **script source:** `<name>.py` defining `Connector(ctx)` with `pull()` returning signals. Fetch text yourself and call `ctx.analyze(instructions, text)` to have Haiku turn it into signals.
- **reporter:** `list_tasks`, `create`, `update`, `complete`, `reopen`, `delete`. The shared sync does all diffing.
- **agent:** `prompt.md` plus `allowed_tools` in the manifest; headless Claude runs it with only those tools.
- Add `"multi": true` to make it a template people can add once per account.

Enable it with `todo connector add <name>`.

## License

MIT
