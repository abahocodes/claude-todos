Check the user's Gmail for changes to their todo list. Today is {today}. Look only at mail since {since}.

You may only read. Never send, reply, draft, label, archive or delete anything.

1. Search for threads since {since} where someone asks the user to do something concrete
   (a request, a deadline, a deliverable, a form to fill, a payment, a review), e.g. queries like
   `after:{since} -category:promotions -category:social` and `after:{since} is:important`.
   Ignore newsletters, receipts, notifications, marketing, and automated alerts.
2. Search for mail the user SENT since {since} (`in:sent after:{since}`). A sent reply that clearly
   delivers what an open todo asks for is evidence that todo is done.
3. Open only the threads you need. Quote short evidence.

Current todo list (ids are what you reference):
{todos}

Rules:
- "new" only for a concrete ask directed at the user that is not already on the list. Title it as an outcome
  ("Send W-9 to the accountant"), set "due" when a date is stated, put the thread subject in "evidence".
- "done" only when the user's own sent mail clearly completes a listed open todo.
- Put the Gmail thread id in "ref" as "gmail:<thread id>".
- At most 10 signals. When unsure, lower the confidence rather than skipping something important.

{schema}
