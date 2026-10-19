# A saved request: every state, every transition

A request a person sends before the Host's tools have loaded is kept (the user
row and a system note in the thread) and queued as a wake: a `calendar_events`
row, `event_type='scheduled_task'`, `payload.saved_request = true`
(`brain/mastercode/runtime/identity_hold.py::save_refused_request`). This page
is the whole map of what can happen to such a row. It is pinned against the code
by `scripts/test_saved_request_state_map.py`: every statement in `server/` and
`brain/` that writes `calendar_events` must be in the table below, and every row
of the table has a scenario in `scripts/test_tools_before_first_turn.py`
(`SM_SCENARIOS`) that starts a saved request in that state, makes the event
happen, and checks the invariant.

## The invariant

**A saved request that leaves `running` or "taken over" without a recorded
"nothing acted" outcome goes to `held_for_person` with `crash_unknown`, and is
never scheduled again.** Scheduled work runs by itself; a request whose turn may
have acted (the process died, or no outcome was recorded) must not be redone
without the person's say. Only the person's next message takes a held request,
and the model is told it was interrupted and to check before redoing it.

"Nothing acted" is recorded only by: the runner finding no free turn slot
(T04), the turn being refused before it ran (identity not loaded, no chat model:
T05), a PII plan not yet landed (T06), and a takeover whose turn ended before
any tool changed anything (`Takeover.settle`: T14, T15). Everything else
(a stuck `running` row, a stale takeover, a restart) has no such record.

## States of the row

| State | Stored as |
|---|---|
| scheduled | `status='scheduled'` (not yet due, or due) |
| running | `status='running'` (the runner claimed it) |
| taken over | `status='cancelled'`, `cancelled_reason='a newer message in this thread took it over'`, `payload.taken_boot = <process id>` |
| held for the person | `status='cancelled'`, `cancelled_reason='held_for_person'` (+ `payload.crash_unknown` when whether it acted is unknown) |
| settled | `status='cancelled'`, reason carried forward / the turn that had it acted / the person stopped the turn |
| completed / failed | `status='completed'` / `'failed'` |
| cancelled | `status='cancelled'` (the person, the Host, the Agents app, a deleted thread) |

## Transitions

| ID | From → to | Code site | Triggered by | Outcome recorded / invariant |
|---|---|---|---|---|
| T01 | (new) → scheduled | `server/bridge/memory_handlers.py::handle_calendar_append` | the person's refused message (`save_refused_request`) | a new request, nothing has run |
| T02 | scheduled → running | `brain/mastercode/brain/offline_task_runner.py::run_offline_tasks_loop` | the runner's poll claims a due row | none yet: the row is now `running` |
| T03 | running → running (alive) | `brain/mastercode/brain/offline_task_runner.py::_touch` | a steps task between steps | status unchanged |
| T04 | running → scheduled | `brain/mastercode/brain/offline_task_runner.py::_requeue` | no free turn slot, before any step ran | nothing acted (the turn never started) |
| T05 | running → scheduled (delayed, counted) | `brain/mastercode/brain/offline_task_runner.py::_defer_for_identity` | the identity hold refused the turn, or there is no chat model, or the tools are not there | nothing acted (refused before the model) |
| T06 | running → scheduled (delayed) | `brain/mastercode/brain/offline_task_runner.py::_wait_for_plan` | the PII plan row has not landed | nothing acted |
| T07 | scheduled (payload only) | `brain/mastercode/brain/offline_task_runner.py::_plan_confirmed` | the PII plan row landed | status unchanged |
| T08 | scheduled (payload only) | `brain/mastercode/brain/offline_task_runner.py::_fold_into_newest` | a wake finds a newer message and folds its words into the newest held request | status unchanged |
| T09 | running → completed / failed (one-shot); running → scheduled (recurring re-arm, never a saved request) | `brain/mastercode/brain/offline_task_runner.py::_finalize` | the wake's turn ended and was reported | a recorded result |
| T10 | running → cancelled | `brain/mastercode/brain/offline_task_runner.py::_run_turn` | the thread it was set in was deleted | terminal |
| T11 | running (stuck) → held_for_person + `crash_unknown` for a saved request; → scheduled for any other task | `brain/mastercode/runtime/identity_hold.py::sweep_stuck_running` (called by the runner's `_sweep_orphans`) | the orphan sweep at start and periodically | NO outcome recorded: a saved request is held, never scheduled |
| T12 | scheduled → taken over; held_for_person → taken over | `brain/mastercode/runtime/identity_hold.py::_take_scheduled` | the person's next turn (or a newer refused message) | records `taken_boot`; `crash_unknown` is kept |
| T13 | running (nothing here runs it) → taken over + `crash_unknown` | `brain/mastercode/runtime/identity_hold.py::hand_over_saved_requests` | the person's next turn finds a stale `running` row | NO outcome recorded: flagged as possibly partly done |
| T14 | taken over → settled / held_for_person | `brain/mastercode/runtime/identity_hold.py::_set_reason` | `Takeover.settle`: the turn ended (carried, acted, stopped, blocked) | the turn's outcome |
| T15 | taken over → scheduled (delayed, counted) / held_for_person | `brain/mastercode/runtime/identity_hold.py::reschedule` | `Takeover.settle`: the turn failed before anything acted; or a "release" (grant landed, chat model saved) | scheduled only if `crash_unknown` is absent; a flagged row goes to held_for_person; a release never makes a flagged row due |
| T16 | taken over (boot id is not this process's, or older than the turn limit) → held_for_person + `crash_unknown` | `brain/mastercode/runtime/identity_hold.py::sweep_taken_over` | the orphan sweep at start and periodically | NO outcome recorded: held, never scheduled |
| T17 | scheduled / running → cancelled; held_for_person → cancelled | `brain/mastercode/circles_threads.py::delete_thread` | the person deletes the thread | terminal |
| T18 | scheduled / running → cancelled | `server/bridge/memory_handlers.py::handle_calendar_cancel` | the person, the Host or the calendar pane cancels | terminal |
| T19 | scheduled → cancelled | `brain/mastercode/runtime/tools.py::_exec_schedule_task` | the Host's `schedule_task` cancel | terminal |
| T20 | scheduled / running → cancelled | `server/routes/agents.py::cancel_scheduled` | the Agents app | terminal |
| T21 | scheduled → cancelled | `server/browser_agents.py::_cancel_event` | a browser agent run is cancelled | terminal |
| T22 | any → completed | `server/bridge/memory_handlers.py::handle_calendar_complete` | the person or the calendar pane marks it done | terminal |
| T23 | (any state) → the same state, `entity_id` renumbered (not a state transition) | `server/host_restore.py::renumber` | a restore of a backup made under another Host number | status, reason and payload are untouched; see "Restore" below |
| T24 | scheduled → scheduled, due now (not a state transition) | `server/host_events.py::emit` | a platform event a `schedule_task` `wake_on` row waits for (connector, NAC, ticket, key, app proof, site) | only a row carrying `payload.wake_on` for that event moves, and only its time (+ `payload.woke_on`); a saved request never carries `wake_on`, so none is ever touched |
| T25 | (any state) → the same state, `payload.origin.thread_id` set (not a state transition) | `brain/mastercode/brain/offline_task_runner.py::ensure_origin_thread` | a Circles task that names no thread writes for the first time (there is no original conversation) | only `payload.origin` moves, to name the topic it now lands in; status and reason are untouched, and a task that names a thread is never written |

Rows that only move a saved request toward scheduled are T04, T05, T06 and T15;
each is guarded by "nothing acted" (T04–T06 by construction, T15 by
`identity_hold.reschedule`, the one owner). Rows that leave `running` or "taken
over" with no recorded outcome are T11, T13 and T16, and all three end in
held_for_person with `crash_unknown`.

## Restore

A restore brings every row back in the state the snapshot held. `host_restore.renumber`
changes only the `entity_id` column (T23: it updates every integer `entity_id` column in the
schema, `calendar_events` among them, by a computed table name, which is why it is in the second
table below). So a restored saved request can come back `running` (its turn died with the
process the snapshot was taken from), `taken over` (taken by a process that is gone), scheduled
or held. The boot sweeps already handle the first two: the orphan sweep holds a stuck `running`
saved request flagged `crash_unknown` (T11), and a `taken over` row whose `taken_boot` is not
this process's boot id is held and flagged at once (T16). A restored scheduled request runs as it
would have; a held one stays held for the person's next message.

## One-time migrations that write `calendar_events`

| ID | File | What it does |
|---|---|---|
| M01 | `migrations/v083_calendar_event_id_unique.sql` | backfills a missing `event_id` and removes duplicate `event_id` rows, once, when the unique key was added; it never changes `status` |

## Writers with a computed table name

A write whose table name is built at run time cannot be read off the code. Each one is listed
here with whether it can reach `calendar_events`; `scripts/test_saved_request_state_map.py`
fails on any computed-table writer that is not in this table.

| Site | Reaches `calendar_events`? | Why |
|---|---|---|
| `server/host_restore.py::renumber` | yes, the `entity_id` column only | T23 |
| `server/host_restore.py::carry_rows` | no | deletes only tables in `CARRIED_TABLES`, which has no `calendar_events` |
| `server/host_restore.py::_settle_keys` | no | inserts rows of tables a carried table's foreign key names; `calendar_events` has no foreign keys |
| `server/applications/installer.py::uninstall_app` | no | deletes by `app_id` from the app registry tables |
| `server/applications/registry.py::uninstall` | no | the same, in the registry |
| `server/applications/subject_import.py::_upsert_sql` | no | a site's rows of the tables its manifest names; none is `calendar_events` |
| `server/network/nac_approval.py::clear_refused_request` | no | one of the two nac request tables, named by the caller |
| `scripts/host_ops_stubs.py::execute` | no | a test stub for host ops, not run on a Host |
