---
name: purse-save-policy
description: What is worth saving to Purse memory — and what to leave out.
version: 1.0.0
updated_at: 2026-09-01T00:00:00Z
---
# Purse save policy

Purse memory is a vault of **durable facts about the user**, shared across every
tool the user connects. Before you call `add_memory`, ask one question:

> Will this still be true and useful weeks from now, in a *different* conversation
> and a *different* tool?

If yes, save it. If it only matters inside the current thread, do not.

## Save these

- **Stable preferences.** How the user likes to work, in terms that outlast this
  task. *"Prefers TypeScript over JavaScript for new projects."* *"Wants commit
  messages in imperative mood."*
- **Durable facts about the user or their world.** Their timezone, their stack,
  the names of their projects and repos, their role. *"Works in the Europe/Berlin
  timezone."* *"Their main app is called Ledger; the API is a Go service."*
- **Decisions worth remembering.** Choices the user made that should hold until
  they revisit them. *"Decided to standardise on PostgreSQL, not MySQL."*
  *"Deploys go out on Thursdays, never Fridays."*

Pick the `kind` that fits: `preference`, `fact`, or `decision`. Write the memory
as a short, self-contained statement that will make sense with no surrounding
conversation — the memory is read back cold, by another agent, later.

## Do not save these

- **Conversation transcripts or turn-by-turn history.** Purse is not a chat log.
  Save the conclusion, never the discussion that reached it.
- **Ephemeral or task-local context.** The file you are editing right now, a
  one-off value, "the user is currently debugging test #4." It is irrelevant the
  moment the task ends.
- **Secrets and credentials.** API keys, passwords, tokens, private keys. These
  belong in the Purse secrets store (behind `use_api`), never in a memory —
  memories are readable content and can be searched and exported.
- **Anything the user has not endorsed as durable.** When unsure whether a
  preference is real or a passing mood, ask, or wait until you have seen it hold
  more than once. A wrong "fact" is worse than a missing one.

## Good vs. bad saves

| Bad (do not save) | Good (save this) |
|---|---|
| "User said 'let's use tabs' in this file." | "Prefers tabs over spaces for indentation." |
| "Currently on line 42 of `main.py`." | *(nothing — task-local)* |
| "OpenAI key is sk-abc123…" | *(nothing — put it in the secrets store)* |
| "We talked about databases for a while." | "Chose PostgreSQL for the project's primary datastore." |

## Keeping memory clean

- **One fact per memory.** Do not pack three preferences into one string; they age
  and change independently.
- **Update, don't duplicate.** If a stored fact is now wrong, use `update_memory`
  to supersede it rather than adding a second, contradictory memory.
- **Prefer the user's own words** for preferences and decisions, trimmed to the
  durable core.

When in doubt, save less. Purse earns its keep by holding the handful of things
that are true every day, not everything that was ever said.
