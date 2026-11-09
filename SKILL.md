---
name: todos
description: The user's personal todo list, shared across all Claude sessions. Use when the user asks about their todos, tasks or what to work on ("what's on my list", "what should I do next", "add a todo", "mark X done", "what's stale", "what should I rethink", "break this down", "review my todos", "approve proposals"), or wants to manage todo connectors (Gmail, Calendar, Todoist, GitHub sync). Not for Claude's own in-session task tracking.
---

# todos

One list shared by every session, stored at `~/.claude/todos/` as an append-only event log.
All reads and writes go through one command. In the commands below, `todo` means:

```bash
python3 <this skill's base directory>/bin/todo
```

## Commands

| Need | Command |
|---|---|
| See the list | `todo list` (`--all` includes done/dropped, `--json` for detail) |
| One todo with history | `todo show 12` |
| Add | `todo add "Send W-9 to the accountant" --due 2026-11-14 [--parent 12] [--notes ...]` |
| Change | `todo edit 12 --title ... --due ... --notes ...` |
| Finish / undo | `todo done 12 --evidence "what proves it"` / `todo reopen 12` |
| Abandon | `todo drop 12 --reason "..."` |
| Push back | `todo defer 12 --due 2026-11-20` (counts toward "rethink") |
| Flags + proposals | `todo review` |
| Proposals | `todo proposals`, `todo approve 3 4`, `todo reject 5`, `todo approve --all` |
| Today's / this week's calendar | `todo agenda` / `todo agenda --week` (`--refresh` re-reads feeds) |
| What the background checks decided | `todo log` |
| AI cost so far | `todo stats` |
| Health | `todo doctor` |

When you act for the user inside a session, add `--by claude`. When the user tells you directly, the default (`user`) is right.

## Reasoning about the list

Start with `todo review`. It prints flags computed from the data; use them, then think beyond them.

- **What to do next.** Overdue first, then due soonest, then anything blocking other todos (children
  point to a parent with `under #N`). Ask how much time or energy the user has only if it changes the answer.
  Recommend one or two, not a ranked essay.
- **Stale** (untouched 14+ days). Ask, per item: still wanted? Then defer with a real date, drop, or
  break it down. Don't silently drop.
- **Rethink** (deferred 3+ times, or 7+ days overdue). Something is wrong with the todo, not the user.
  Name the likely cause (too big, unclear next step, waiting on someone, not actually important) and
  propose a concrete rewrite.
- **Break down** (long or multi-step title, or the user asks). Propose 2 to 5 child todos, each an
  outcome finishable in one sitting, then on approval:
  `todo add "<child>" --parent 12` for each. Keep the parent open until the children are done.
- **Proposals** come from background checks (sessions, email, calendar). Show them grouped, with the
  evidence, and let the user approve or reject in one reply. Never approve on the user's behalf.

Never complete, drop or approve anything the user hasn't confirmed, unless the work just finished in
front of you (then `todo done <id> --by claude --evidence "..."` and say so in one line).

## Alerts

Hooks add a TIME-SENSITIVE note when an event starts within 30 minutes, again within 5 minutes or once
it has started, and once per day for todos due today. When you see one, say it in one short line at the
top of your reply before anything else. Tune with `todo config alerts.lead_min 45` or turn off with
`todo config alerts.enabled false`.

## Connectors

Sources send signals in; reporters mirror the list to an app and send your edits back.

```bash
todo connector list                 # what exists, what's on, last sync, errors
todo connector add gmail            # enable + first sync
todo connector sync gmail --dry-run # see what it would propose, change nothing
todo connector remove todoist       # stop syncing; history kept, id map archived
```

- **todoist** needs a token in the Keychain: `security add-generic-password -s todos-todoist -a todos -w <token>`
  (token from Todoist Settings > Integrations > Developer). Optional project name: `todo config connectors.todoist.project "My List"`.
- **gmail / calendar** use the user's claude.ai connectors, read-only. They never send, reply or change events.
- **github** reads merged PRs through the `gh` CLI.
- **imap** and **ics** are templates for extra accounts: add one named instance per mailbox or calendar.
  The user stores the secret themselves (hidden prompt), then adds the instance:
  ```bash
  todo connector secret personal-mail     # Gmail app password (myaccount.google.com/apppasswords)
  todo connector add imap --as personal-mail --set user=me@gmail.com   # host defaults to imap.gmail.com
  todo connector secret personal-cal      # calendar's private iCal URL
  todo connector add ics --as personal-cal
  ```
  Never ask the user to paste a password or feed URL into the chat; `connector secret` keeps it out of the transcript.
- Your own connector: a folder in `~/.claude/todos/connectors/<name>/` with `connector.json`
  (see the built-in ones in `todos/connectors/` under this skill's base directory).

## Settings

`todo config` shows everything. Common ones:
- `todo config auto_apply true`: let confident inferred changes apply without a proposal (start false).
- `todo config confidence_auto 0.85`
- `todo config throttle.min_chars 3000`, `throttle.min_interval_min`, `throttle.idle_min`
- `todo launchd install|status|uninstall`: the 15-minute sweeper that checks idle sessions and overdue syncs.
