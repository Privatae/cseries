---
name: housekeeping
description: The daily review the person can turn on — you look back over the day's work, improve your own skills, correct memory and finish your own tickets, in one scheduled turn under their limits. Off unless they ask for it.
allowed-tools: schedule_task, task_template, calendar_read, chat_threads, recall, context, memory_curate, tickets, entity_tasks, limits
---

# Housekeeping

You never act on your own. The one exception is work the person asked to
run on a schedule, and this is one such job: a daily look back over what you
did, so the next time goes faster. It is off until the person turns it on,
in their words ("review our work every day", "keep the house clean").

## Turning it on

1. Say what it does, what it never does (below) and what bounds it: each run
   is one scheduled task under the person's limits `scheduled.turns_per_day`
   and `scheduled.allowance_usd` (read them with `limits op='status'`; skill
   'limits'). Say that the run's cost is reported with its result.
2. Ask when it should run, if they did not say. Daily is the default.
3. Create it as ONE recurring prompt task with the steps below:
   `schedule_task op=create title="Daily review" recurrence=daily
   fire_at=<the time they chose> steps=[...]`. Each step is its own turn,
   so five steps cost five turns a day. A recurring task puts a card up for
   the person; it starts only when they choose "Let it run".
4. Tell them it can be stopped any time, from the Calendar or Agents app or
   by saying so.

## The steps

1. **Look back.** Read the day: the threads with new messages
   (`chat_threads`), the builds and agents that ran (`entity_tasks`), the
   scheduled runs and what they cost (`calendar_read op='runs'`). Note what
   took several tries, what failed and why, and what you did more than once.
2. **Skills.** For a job you did more than once whose steps are worth
   keeping, write or revise your skill for it (skill 'storage', "Your own
   skills"; the standard decides what passes). Fix a skill of yours that led
   you wrong today. Nothing else changes a skill.
3. **Memory.** Correct a note the day proved wrong: close it and write the
   right one (`memory_curate`; skill 'memory'). Do not add notes the
   conversations did not support.
4. **Tickets.** Update or close your OWN tickets whose work is done
   (`tickets`; skill 'tickets'). A decision the person has not answered stays
   theirs: never decide, dismiss or re-ask it.
5. **Report.** One short message in the thread: what you changed in skills,
   memory and tickets, what you noticed but left alone and why, and what the
   run cost. If nothing needed changing, say so in one line.

## What it never does

- Start new work: no builds, agents, installs, messages to anyone, or new
  scheduled tasks. Anything worth doing goes in the report as a suggestion.
- Change a limit, a setting, a route or a permission. A limit that got in
  the way goes in the report, and a change to it is proposed only in a
  conversation with the person (skill 'limits').
- Re-read what it already reviewed: each run looks at what happened since
  the last one.
