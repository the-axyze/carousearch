# Knowledge notes

Optional notes of your own that the Ask agent's planner reads when a request is about what a note applies to. carousearch ships none: the agent's method has no knowledge of any kind of item, so what it knows comes from the model's general knowledge, web search, the listings it samples, and these notes.

Everything in this folder except this README is ignored by git: the notes are yours.

## Format

One topic per file, short markdown, with front matter naming the words that select it:

```markdown
---
applies_to: standing desk, desk frame
---
Frames with two motors keep their resale value; single-motor frames sell for about half.
Sellers usually state the frame width in the title ("140cm").
```

- `applies_to`: comma-separated keywords (or a `[bracketed]` list, or `- item` lines below the key). A note is used when one of its keywords appears, as whole words and ignoring case, in your request or in the plan's item class.
- A file without `applies_to` is never used.
- Keep a note to facts and preferences ("what drives value", "how sellers phrase X", "I never want Y"). The planner treats notes as the owner's knowledge, names them in the plan's assumptions when it relies on them, and does not let them change its method.

## Where notes come from

Write them by hand, or save one the agent suggests: after an Ask run in the web app, a "Suggested note" box may appear with a **Save to knowledge/** button. Nothing is saved unless you press it, and an existing file is never overwritten (a new one gets a `-2` suffix).
