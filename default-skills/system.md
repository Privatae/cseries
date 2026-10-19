---
name: system
description: How to respond when something in your pod misbehaves — sidecar crashes, expired auth, bridge unreachable. Operational doctrine that the host executes WITHOUT paging the user for every glitch and WITHOUT falling back to inappropriate substitute tools.
allowed-tools: system.restart_app, system.app_status, explore_environment, kernel_op, report_crash, inbox_send, tickets, capabilities
---

# System SOP

This is your playbook for when your pod misbehaves. Sometimes the pod's
infrastructure misbehaves — a coding harness sidecar crashes, an OAuth
token expires, the bridge is unreachable. The
supervisor surfaces these events as inbox messages with
`sender_type='system'`. This file is the playbook.

## Sidecar crash

You'll receive an inbox message shaped like:
```
{type: "sidecar_exit", app_id: "claude-code", exit_code: -9, signal: 9,
 crash_file: "crash/core.837.1755500000", bytes: 1207959552, mtime: 1755500000}
```
`crash_file`, `bytes` and `mtime` are present ONLY when the crash left a core
dump. `signal` is set when the process was killed by one (exit_code is then
negative); a plain non-zero exit_code has `signal: null` and never has a dump.

**Response policy:**
1. First crash of the same app within 5 minutes: call
   `operate(tool='system.restart_app', args={app_id})` once. Wait for `system.app_status` to
   report `running`. If yes, no further action.
2. Second crash of the same app within 5 minutes: do NOT auto-restart
   again. Report it to your user: in your reply when they are here, and
   with `inbox_send` (subject + markdown body, through `operate`) when the
   crash happened while they were away. Summarize the crash + last logs,
   marked as a warning.
   Stop attempting to use that harness until the user acknowledges.
3. Crashes spread across more than 5 minutes apart: each one is treated
   as the first crash above.

Do NOT spend tokens diagnosing a known crash loop. Surface what the
event gives you — app_id, exit_code, signal, and `last_logs` (the tail
of what the process printed before it died; since 2026-09-03 an
in-container process app's stdout and stderr land in its log file, and
this event carries the last lines). `system.app_status(app_id)` returns
the same tail as `log_tail` at any time, plus `ready` — whether the
app's own ready_path answers right now. A process can be alive while
the app is broken (a UI server up over a failed bootstrap):
`running: true` is not "it works"; `ready` and `log_tail` are.

## Crash dump on disk

When a `sidecar_exit` carries `crash_file`, the crash left a core dump — a
snapshot of everything that process had in memory when it died.

**It is the user's file, and only the user's.** The platform cannot read it,
does not back it up, and never ships it anywhere. Say that plainly, because it
is the first thing a person wonders. Do not open it, do not summarize it, do
not copy it out of the pod.

**Response policy:**
1. Tell the user in your reply, once, in plain words: what crashed, where the
   dump is, how big it is. "claude-code crashed and left a 1.2 GB crash dump at
   `crash/core.837.1755500000`. It's a snapshot of what that program had in
   memory — it's yours, nothing leaves your machine." Keeping or deleting it
   is theirs: put it in the question box (`escalate`: keep / delete).
2. Act on their answer when it comes back into the turn. Keep = do nothing. Delete =
   `operate(tool='kernel_op', args={method:'DELETE', path:'/api/system/crash', body:{name:'<name>'}})`.
3. To see what is already there:
   `kernel_op(method='GET', path='/api/system/crash')` — names, sizes and times
   only, which is all that op can ever return.
4. Do not nag. Old dumps age out on their own: the newest 4 GiB is kept and
   anything past that is dropped automatically.

**If the crash looks like a PLATFORM defect**, draft the report
(`tickets` op='support_open'): it is a DRAFT until the user approves what it
shares on its own card, so nothing leaves without their click. It counts as a platform defect when the thing
that died is something the platform ships, not something the user wrote:
  - a bundled binary or harness (`claude-code`, `codex`, `cursor`, `grok`,
    `opencode`, `kimi`, `antigravity`)
  - a kernel component (the Privatae Host server itself, the pod's Postgres)
  - a sidecar image the platform publishes
A crash in the user's OWN program is not a platform defect. Help them with it
directly; do not report it.

To report: `operate(tool='report_crash', args={component, signal, exit_code,
dump_bytes})`. That call sends METADATA ONLY — which component, which image
version, the signal and exit code, when it happened, how big the dump was. It
carries no filename fragment, no log line, no path from inside the user's work,
and never any part of the dump. Say that to the user before you send it, in
those terms, so consent means something. If they decline, drop it.

The report goes to the platform operator, who sees a defect signal — the same
component crashing across more than one machine is how a shipped bug gets
found. Nobody gets access to the dump; if the fix needs more, the operator asks
the user, through you.

## Research returned status='unavailable'

`research` runs on the routed model with the provider's own web search. When
no route can search, it returns `status='unavailable'` with ONE actionable
sentence in `result` (it names the host model in the Orchestrator). Say that
sentence to the user; do NOT fall back to local_grep or code_read in place of
research — they answer different questions.

## Bridge unreachable

A bridge op returns network error / timeout.

**Response policy:**
1. Do NOT retry inference in a loop. The bridge being down means
   nothing is going to work anyway.
2. Tell the user directly (error): "Platform bridge unreachable.
   Your work is queued locally; I'll resume on next successful ping."
3. Resume normally on the next successful bridge call.

## When NOT to escalate

- Routine completions that succeed: don't alert.
- A single bridge timeout that resolves on retry: don't alert.
- A crash that auto-restarts cleanly per the policy above: don't alert.

The handler doesn't want a notification every time something blips.
Escalate only when the system needs human attention OR when you've
exhausted what the SOP allows you to do automatically.

## Tools

- `system.restart_app` (args `{app_id}`, through `operate`) — restart a sidecar your pod owns
- `system.app_status(app_id)` — query state (running/crashed/stopped)
- `explore_environment()` — installed apps + a one-line summary of each
- `kernel_op(method='GET', path='/api/system/crash')` — list this pod's crash
  dumps: name, size, time. Metadata only, by construction.
- `kernel_op` with `{method:'DELETE', path:'/api/system/crash', body:{name}}`
  (through `operate`) — delete one, at the user's request
- `report_crash` (through `operate`) — send a METADATA-ONLY crash report to the platform when
  a platform-shipped component crashed AND the user consented. Never for the
  user's own code.

Anything user-facing goes in your reply when the person is here, and to
their inbox with `inbox_send` when they are not.


## system.app_status — guidance

A process can be alive while the app is broken — a UI server up over a
failed bootstrap looks "running" to the supervisor — so read `ready` and
`log_tail`, not `running`, before saying an app works. Read this before
restart_app, and quote log_tail to the user when something failed: it names
the failing command (stdout and stderr both land in the app's log file).
Pod-backed apps (runtime.image) are covered too: the runtime pod streams what
the app prints back here, and `last_exit` carries the process's exit code — a
pod can stay 'Ready' on its probe while the app inside has died, and this is
where that shows. `system.start_app` switches a resident app ON (stays up
after every window closes, restarts on a crash, starts at boot);
`system.stop_app` switches it OFF; both are schedulable as schedule_task's
op_tool. `system.restart_app` is for a sidecar whose /status reports degraded
or after a config change it needs to reload.

## report_crash — guidance

METADATA ONLY: this sends the component id, its image version, the signal and
exit code, when it happened, and how many bytes the crash dump was. It cannot
send the dump, a log line, a filename or any path from the user's work —
there is no field for them. Ask the user before calling it and tell them
exactly that list; if they decline, do not call it. Open to every host (no
enrolment), rate-limited per host. Repeats of the same
component+version+signal inside 24h fold into one report with an occurrence
count rather than filing again. The operator sees a defect signal — the same
component crashing on more than one machine is how a shipped bug gets
caught. Nothing fills component_version in for you — omit it rather than
guessing; an omitted version still folds and still counts.

## kernel_op — guidance

Deny-by-default: anything outside the exposed set is refused for every
caller, an agent included. Use your named tools; if none covers it, say so
and file it (`tickets` op='support_open', category feature). Check the capabilities doc §7 (capabilities
op='section') for the exact routes before calling.


## Parameter notes (full text of shortened argument descriptions)

- **report_crash** `component_version`: Image tag/version of the component, if you know it. Omit rather than guess — an omitted version still folds and counts toward the signal.
- **report_crash** `component`: The platform component that crashed — an app/harness id (e.g. 'claude-code'), 'kernel', or 'postgres'.
- **kernel_op** `path`: Exact route path, e.g. '/api/applications/sites/customers' — no templates, no query string (use params).
