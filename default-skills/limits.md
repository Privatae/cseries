---
name: limits
description: The person's limits on spend, turns, builds and other work — reading them, what to do when one stops your work, and asking the person to change one with the limits tool. Only their click changes a limit.
allowed-tools: limits, schedule_task, escalate
---

# Limits

Every limit that can stop work the person asked for is theirs: what a reply
or a scheduled turn may spend, how many turns a task may run, how many
repairs a build starts by itself, how long a build or a browser agent may
take, and the rest. Each has a default the platform ships with; the person
can change any of them in Settings > Limits. 0 means no limit.

You can read every limit and recommend a change. You can never change one.

## When a limit stops your work

The message names the limit, like this:

    Stopped by the limit `build.idle_minutes` (how long a build may show no
    output, CPU or disk activity: 10 minutes). ...

Then:

1. Say what stopped and what was kept (the work's record, its worktree, what
   it found) in a sentence the person can act on.
2. Decide whether the limit was right. Often it was: a build that sat silent
   for ten minutes may well have been stuck. Say so, and do not propose a
   change.
3. If the work was legitimate and the limit cut it short, propose the value
   you recommend: `limits op='propose' name='build.idle_minutes' value=30
   reason='The test suite runs 20 minutes without printing'`. Pick the value
   from what the work actually needed, with room, not a round guess. The
   reason is one line the person reads on the card.
4. To carry on once they decide, schedule the follow-up on the change rather
   than a time:
   `schedule_task op=create ... wake_on={"event": "limit_changed",
   "match": {"name": "build.idle_minutes"}}` with a deadline you would wait.
   If they keep the limit, the task runs at its deadline; say what you will
   do then.

## Reading them

`limits op='status'` lists each limit: its value, the default, who set it
(`you` = the person, `operator`, or `default`), what happens when it binds,
and when it last stopped something. Read it before you tell the person what
a limit is; never quote one from memory.

## What not to do

- Never work around a limit: no splitting a job into pieces to stay under
  it, no re-running a stopped run unchanged hoping it fits. Ask.
- Never propose raising a limit just because one was reached. Propose when
  the work needed it.
- A spending limit is the person's money. Say what the higher value would
  have cost, when you know it.
