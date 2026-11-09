Check the user's Google Calendar for changes to their todo list. Today is {today}.

You may only read. Never create, edit, delete or respond to events.

1. Call list_calendars, then list events from {today} through the next 7 days on EVERY calendar it returns
   except holiday and other public calendars (pass each calendarId to list_events; the default is only the primary one).
2. Look for: deadlines or due dates written as events, meetings that clearly need preparation
   (presentations, reviews, interviews, demos, submissions), and events that match an open todo.
3. Ignore routine recurring meetings, holidays, focus blocks, and events with no action for the user.

Current todo list (ids are what you reference):
{todos}

Rules:
- "new" for a prep task or deadline not already on the list, e.g. "Prepare demo for Thursday review",
  with "due" set to the event date (or the day before for prep), and the event title in "evidence".
- "update" with fields.due when an event clearly sets or moves the due date of a listed todo.
- Put the event id in "ref" as "calendar:<event id>".
- At most 8 signals.

{schema}

In the SAME JSON object, also return "agenda": every event from step 1 (all calendars, next 7 days,
excluding holidays), as [{"start": "<ISO 8601 with UTC offset>", "end": "<ISO 8601 with offset or null>",
"title": "<event title>", "all_day": true|false}]. This list is used for reminders, so include
routine meetings too.
