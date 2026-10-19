---
name: scheduling
description: The host's own clock — how it schedules offline work (a governed model turn, or one deterministic operation with no model), templates that stamp tasks out, reads what a standing task actually did, manages its task queue, and delivers the weekly state-of-the-host changelog.
allowed-tools: schedule_task, task_template, entity_tasks, calendar_read, host_changelog, browser_agent, system.start_app, system.stop_app, inbox_send
---

# Scheduling SOP

## schedule_task — guidance

A fired task IS auto-run with nobody watching: a background loop claims each
due task and runs your prompt through the SAME governed pipeline as a live
chat turn — your identity, your tools, the CEIGAS gate — then reports the
result INTO THE THREAD YOU SET IT IN, so a check you promise here comes back
here.

THIS IS YOUR OWN CLOCK, and the short one-shot use is the one that gets
forgotten: when something needs time before it can be judged — an app still
booting, a build still running, a bot that has not printed its first
heartbeat — do not tell the user you will look again later and then end the
turn, because nothing will bring you back. Set fire_at to when it will
actually be worth looking (minutes is fine; the runner polls every 30
seconds) and tell them the time you will look. Recurring tasks re-arm to
their next occurrence and catch up if the runner was down; one-time tasks
complete.

**Waiting for something to HAPPEN is not a fixed time.** When what you are
waiting for is an event (a key landing in Keys, an app's proof passing, a
site going live), pass `wake_on: {event, match}` with `fire_at`.
The task then runs the moment the platform records that event, and `fire_at`
becomes its DEADLINE: if the event has not come by then, it runs at the
deadline instead. When it runs, its first line tells you which: "The thing
you were waiting for happened: …" with the event's ids, or "Your deadline for
… arrived, and no … was recorded since you set it". A deadline is not proof
that the thing did not happen, so read its state before you tell the person
anything. You pick the deadline: the
longest you would wait before looking yourself. A fixed `fire_at` alone is
for things only time brings (a build still running, an app still booting, a
reminder at 9:00). `wake_on` is one-shot: it cannot carry a recurrence, and it
runs a prompt (not `op_tool`), so the turn can see which of the two happened.

Only what happens AFTER you set it wakes the task, so read the state first
(`nac_status`, the ticket, Keys) and do not wait for something already done.
`match` holds equality keys, all of which must equal the event's; an empty
match wakes on any event of that kind. The events and their keys:

| event | keys | when |
|---|---|---|
| connector_paired | grant_id, app_id, platform, ticket_id | an account sent the pairing code; it waits for the person's approval (ticket_id is the card) |
| connector_approved | grant_id, app_id, platform | the person let the account in; the grant is live |
| connector_declined | grant_id, app_id, why | the pairing closed unapproved: why = declined, wrong_codes, replaced or revoked |
| connector_expired | grant_id, app_id | the pairing code expired unused; recorded when the pairing is next read (Network, the code shown) or a code arrives, not when the clock runs out |
| connector_revoked | grant_id, app_id | a live connector grant was revoked |
| nac_approved | kind, id | a NAC draft was approved (kind = policy, egress, mgmt_grant, dcn_store, dcn_move_plan) |
| nac_revoked | kind, id | a NAC rule was revoked or a draft declined |
| ticket_decided | ticket_id, status, option_id | a ticket was decided (status decided, option_id picked) or dismissed |
| key_added | name | a key was stored in Keys under that name (never its value) |
| app_proven | app_id, project_id, task_id | an app's post-ship proof passed |
| app_proof_failed | app_id, project_id, task_id, verdict | an app's post-ship proof did not pass |
| site_published | project_id, repo, slug, hostname | a site was published |

A ticket that EXPIRES unanswered (an op card whose run lapsed or was
replaced) was not decided, so `ticket_decided` does not fire for it: a task
waiting on that card runs at its deadline.

A build or an agent you started needs no wake: its result already starts a
turn in the thread when it finishes. The one exception: after three failed
runs in a row in the same thread, a further failure is written there but
starts no turn. A connector pairing needs no wake for its answer either: the
person's approve or decline, on the card in the chat or in the Network app,
comes back into the conversation that started it as their answer, which
starts your turn. A wake on `connector_approved` would run a second turn for
the same approval. The pairing's code card is in the chat and in the Network
app; never ask for the code.

`wake_on` is for what has no answer path of its own. Example: you asked the
person to put their Telegram bot token in Keys under `TG_BOT_TOKEN`, so you
can pair the telegram app. Storing a key starts no turn, so do not set a
check for fifteen minutes from now. Set:

    schedule_task op=create title="Telegram token stored"
      fire_at=<a day from now>
      wake_on={"event": "key_added", "match": {"name": "TG_BOT_TOKEN"}}
      prompt="TG_BOT_TOKEN for the telegram app: if it is in Keys, start
              the pairing (propose_nac_policy kind=connector app_id=telegram
              platform=telegram token_name=TG_BOT_TOKEN) and tell the user
              the code card is in the chat; if it is not, remind them where
              to add it."

If neither an approval nor a decline arrives, check once after the pairing
runs out: `schedule_task op=create` with `fire_at` just after the pairing's
`expires_at` and no `wake_on`, and a prompt that reads `nac_status` (reading
it brings the pairing's expiry up to date). Cancel that check (`op=cancel`
with its `event_id`) when the answer arrives. Do not wait on
`connector_expired` for this: it is only recorded when something reads the
pairing, so it does not fire on time.

Write `prompt` as instructions to your FUTURE self at a moment when the user
is not there to answer a follow-up: name the site, the account, the file,
rather than referring to "the one we discussed".

**Two lanes, and picking the right one matters more than anything else
here.** If the work is DIFFERENT every time and needs your judgement, pass
`prompt` (and optional `steps`): each fire runs a governed turn and costs
tokens. If the work is the SAME every time — one operation whose exact
arguments you already know — pass `op_tool` + `op_args` instead: it fires the
operation directly with NO model in the loop, so it costs nothing and cannot
drift. Prefer the op lane whenever it fits. A repetitive task is not
something to re-run yourself on a schedule; it is something to BUILD once and
let run. `op_tool` is exclusive with prompt/steps/template_id, and only ops
that finish without a person present can be scheduled — an operation whose
result someone has to act on (opening a window, returning a login URL) is
refused, because it would not work, not because it is unsafe; the error names
the ones that are accepted. `op_args` are frozen at scheduling time and not
re-interpreted at fire time — state them completely. `template_id` stamps out
a task_template: its prompt + steps render into the payload, and its
recurrence is inherited unless you pass your own.

**The prompt lane has a daily ceiling; the op lane does not.** A prompt task
may run at most `max_runs_per_day` MODEL TURNS in a rolling 24 hours — 200 by
default, which is more than twice what a quarter-hourly schedule needs.

The budget counts TURNS, not fires, and the difference matters the moment you
pass `steps`: **each step is its own governed turn**, so one fire of a 20-step
task costs 20 turns. That task every 15 minutes is 96 fires but 1,920 turns,
and it is refused at create time even though 96 is well under 200.

At the ceiling the fire is skipped, the run history records why in plain
English, and the task re-arms on its own recurrence — never faster. A skip is
not a failure and is not counted as one. Creating a prompt task whose
recurrence would spend more turns a day than its ceiling is REFUSED at create
time with both numbers in the error: raise `max_runs_per_day` if the user
genuinely wants that many turns, use fewer steps, or space the recurrence out.
If the work is identical every time the answer is none of those — it is
`op_tool`, which runs no model and has no ceiling at all. Passing
`max_runs_per_day` alongside `op_tool` is refused rather than ignored, for the
same reason.

## calendar_read — guidance

Use the calendar list/today views first to find an event_id, then call this
to inspect a single event. `op='runs'` answers whether the host's standing
work is actually working — for a recurring task, this is the only place the
answer lives, and you should check it before telling the user something is
running.

The Calendar app shows the user's events and your scheduled tasks on one
calendar. To put one event in front of the user, `open_app` with app_id
'calendar' and view 'event:<event id>'; one day is view 'day:YYYY-MM-DD'. A
task the user cancelled there stays cancelled; set it again only when they
ask.

## host_changelog — guidance

The pushed, dated, attributable record of what changed in YOU since the last
one, so the user never has to go looking. It reads five of your own tables
since the last changelog (or the last 7 days) and writes ONE Inbox message:
behavioural rules added (with their origin) and rules promoted to hard by the
6-hour promoter; coding-harness runs per engine with failures and cost;
memory formation (new entries, consolidation passes, whether the loop is
healthy); and a NOT-SET-UP-YET section — embeddings, standing tasks, projects
without a plan, storage on the default backend — so an empty state is never
mistaken for a broken one. "Nothing changed" is a valid changelog and is
still delivered. It never reports personality drift: your weights change only
when the user moves the sliders. Deterministic and model-free, which is why
it runs as a standing op: every host is seeded with a weekly one at boot, and
the user can stop it from the Agents app.


## Parameter notes (full text of shortened argument descriptions)

- **schedule_task** `op_tool`: create, the deterministic lane: one operation to run at each fire with no model. Exclusive with prompt/steps/template_id; only unattended-safe ops are accepted.
- **schedule_task** `op_args`: For create: the exact arguments for op_tool, frozen at scheduling time. They are not re-interpreted at fire time — state them completely.
- **schedule_task** `template_id`: create (optional): a task_template id to stamp out — its prompt + steps render into the payload; its recurrence is inherited unless you pass your own.
- **host_changelog** `deliver`: Write the changelog to the user's Inbox (default true). false = return the rendered changelog without delivering it.
- **schedule_task** `recurrence`: For create (optional): 'hourly' | 'daily' | 'weekly' | 'monthly' | 'every:<seconds>'. Omit for a one-time task.
- **schedule_task** `max_runs_per_day`: For create on the PROMPT/STEPS lane (optional): the most model turns this one task may run in a rolling 24 hours. Defaults to 200. A recurrence that would fire more often than this is refused at create time, naming both numbers. The op_tool lane runs no model and has no ceiling.

## The thread is your chain of thought

When you set a task, its plan is written into the thread you are in, where
the user reads it. When it fires, you are pointed back at that thread: you
think there, continue from what it shows, and report there. Nothing else is
carried between turns.

- **Write the plan for the thread.** The prompt or steps are what the user
  sees now and what you continue from later. Say what you will check and
  what you will do with the answer.
- **Keep updates short.** On a long-running task each wake-up reads the
  thread so far. Report what changed since the last update, not the whole
  story again.
- **Turn types.** A task runs under the type of the turn that sets it: that
  type's tools, keys, servers, skill and model. A typed turn cannot set work
  for another type; say in the thread what that work is instead.
- A task whose type is no longer active does not run; the run record says so.
- `calendar_log` records events. It does not schedule work.

Several tasks due together run side by side, each in its own lane.
