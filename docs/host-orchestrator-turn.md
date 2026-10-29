# The Host's orchestrator turn

Code runs only through the host's own tools (`build`, `ladder.run`, `mesh_harness_run`,
`lab_run`, `machine_exec`, and `mgmt_run` for an op it wrote); a **dispatched agent**
(`runtime/scoped_agent.py`) only reads.

> In this document "the platform" means the C-Series kernel inside the Host: the code outside
> the Host's own `world` schema. It does not mean a hosted service; C-Series has no hosted Host.

The host is the **orchestrator** of its turn:

> build context → think it through → pick tools → write the agent's prompt → dispatch a
> scoped agent → monitor it → integrate the result.

The host runs its own operations with `operate` (found with `find_tools`) and reads with
`read`; it dispatches an agent only to read content it does not own.

## The turn

1. **Screen + record** — `input_guard.sanitize` (structural injection screen); write the user
   turn to memory (async embed). No inference.
2. **Build context** — deterministic: system prompt (host identity/surface) + a fixed recent
   recall window + relevant memory. The host gathers context itself.
3. **Govern (structural, no LLM)** — the CEIGAS inference-crossing gate; each dispatched agent
   holds only the read capabilities its grant names. Replaces the per-turn LLM veto entirely.
   The crisis check (SB 243 floor, `runtime/crisis.py::detect_crisis`, on the person's words:
   the turn's message — not the brief of a turn the platform started, unless it is a request the
   person saved — and each nudge) never gates the model and never writes as the Host: on a hit
   the platform writes its content-free governance row, sends the `governance` frame (Circles and the Workbench show the
   platform's own dismissable notice with the 988 resources), and puts a "## Crisis Check" fact in
   that turn's context; the model reads it and answers in its own words.
4. **Orchestrate (the one host call + loop)** — the host model reasons and acts via its
   orchestration tools:
   - `recall(query)` — pull more context on demand.
   - `dispatch(intent, task, tool_scope, grant, system_prompt, work)` — a background agent
     that reads untrusted content and reports back (see below); the host writes its prompt,
     picks its tools, monitors it, integrates the return.
   - `build(action, …)` — the **harness-pool handoff**: building/changing/continuing CODE on a
     coding harness (claude-code/codex/cursor/…). Code work goes here, NEVER through
     `dispatch` — a dispatched agent can never execute code.
   - `escalate(...)` — hand a decision to the user.
   Trivial conversational turns are answered directly, with no dispatch.
5. **Stream + record** — existing AI-SDK frames; write the assistant turn (async).
6. **No post pass** — memory is written inside the turn; nothing re-reads the conversation
   afterwards.

## Two kinds of delegation — DO NOT conflate
- **The harness pool — external coding harnesses** (`build`, `ladder.run`;
  claude-code/codex/cursor/…): run **outside** the Host container, in their own
  containers, for **code/build only**. Governed by the container/OCI boundary + signed grant. Only the
  host's OWN build tools reach them; the host hands off to them for code, nothing more.
- **Dispatched agents — in-container scoped agents** (`runtime/scoped_agent.py`, live):
  ephemeral agents inside the Host container that read untrusted content (a foreign repo, the
  web, a message) with only the read capabilities their grant names, and report back. This is
  what `dispatch` spawns. Reuses the tool loop's parts (`provider_gateway.call_model` and its
  own loop) with a scoped tool subset + task prompt; ephemeral; returns to the host.

`dispatch` always spawns an in-container agent, holding only the readers for its one task.
Latency note: a dispatched agent is in-process (no container cold-start); the harness pool's
container cost is paid only for code/build.

## Safety model — the pool/agent separation IS the security boundary
Dispatched agents are **the host's readers inside its own container**: they read untrusted
content and **CANNOT execute or build code**. A dispatched agent reads and reports; it cannot
change anything, and there is no code execution in its reach. **All executable code goes
through the host's own tools**: the harness pool (disposable harness container, OCI boundary +
signed grant), a mesh or lab server, or a machine: a root command its bastion granted, or an op
the person approved there. Enforcement: a dispatch `tool_scope`
may NEVER contain a code-exec/build tool (`build`, `ladder.run`,
`review.compare`, `review.propose_commit`, `mesh_harness_run`, `bash`, …) — the runner
hard-strips/refuses them. Capability-limited hands in-container +
isolated code execution in a container = the thing that keeps it safe.

## Background + non-blocking — the chat never blocks
`dispatch` is **fire-and-forget**: it spawns the agent as a background task, registers it,
and returns immediately. The agent runs on its own; its result comes back to the Host (see the
async lifecycle below). "Constant chat, no waiting" still holds for the person: a turn that is
holding for work it started takes their words at once (they reach it as a nudge), and they can
stop it at any time.

## Async lifecycle — the turn holds for what it started
The turn holds open until the work it started is done, so the Host acts on the result with its
tools and a warm cache:

1. **Request → start → hold.** The Host starts the work in the turn — `build` on the harness pool,
   `dispatch` for an agent, or `escalate` for a question only the person can answer (the ask
   card). The turn then HOLDS at its exit for what it started (`session.hold_for`): at most
   `HOLD_S` (240 s) per turn, inside the 5-minute prompt-cache window and far under
   the turn's idle window (`turn_registry.TURN_IDLE_S`: a turn is stopped only after 30 minutes
   with no sign of life — there is no total unless a Host sets `CSERIES_CHAT_TURN_TIMEOUT_S`). Before it waits, it **writes its reply as it stands and the
   close_turn handoff** (`memory_engine.write_turn_row_by_token`, `held` until the turn ends, then
   rewritten in place — one row per turn, stamped when the reply began), and the client gets a
   **waiting card** (a `waiting` tool card naming what it waits on) that completes when it stops.
   The hold never takes the person's composer: while the card is up, Send stays Send (Stop beside
   it) and what they type goes into the holding turn (`POST /api/brain/turns/{id}/nudge`, Circles'
   nudge), held by the server (`pending_words`) to go as the next message if the turn ends first. Every word sent into
   a running turn is written as a user row when the turn reads it, stamped when it was sent. A turn
   that has STOPPED READING — it released its holds at its exit (`session.release_holds`; going
   round its loop again reads again) — is never pushed into: a different message on its thread
   starts as a turn of its own, registered at once and run once that turn is done
   (`turn_registry.start_or_attach`, bounded by `FOLLOW_ON_WAIT_S`), the per-turn nudge route
   answers 409 (the client sends it as the next message), and Circles' nudge starts that turn in
   the room the registry recorded for the session (`answer_after_turn`). The
   wire stays alive on its own (`brain_service._never_silent` keepalives).
2. **One background budget, live chat always has a slot.** Holding turns and woken turns are
   counted against `session.max_holding_turns()`, how many more turns the Host's memory holds
   now (`host_memory.turns_that_fit`). A person's message never needs a slot.
3. **What arrives enters the running turn.** A finished build or agent is handed to the turn that
   started it (`hand_result_to_turn`) as a platform brief with the run's facts; the person's answer
   to a ticket is spoken into the turn that asked it by the decide itself
   (`tickets.decide_ticket_service` → `answer_into_turn`, whole, like a nudge), whichever
   surface they answered on. Both go through the same drain as a nudge, and the Host carries on
   **with its tools and a warm cache** — the next phase, a retry, a report. A result that arrives
   while the model has a round pending waits for that round; the person's words do not. A turn
   that resumes this way does not re-arm the go-ahead guard (one repair per turn; an ask written after its repair is counted in the record, not repaired), and the
   next close_turn's handoff replaces the one recorded before the hold (its writes still run
   once; none → the stale handoff is not handed on).
4. **Past the hold, a real turn wakes.** A result that lands after the turn ended writes a fixed
   line in the originating thread and wakes a REAL Host turn there (`continue_after_result` →
   `_wake_host_turn`, the same waker as the research round's continuation): the normal send path,
   a system turn, with the thread's goal and handoff and the result as what happened. It carries on
   like any turn, holds for what it starts, and the go-ahead guard covers it. The work's own words
   in its brief (an agent's report, a harness summary and transcript tail) are screened and fenced
   as data (`agent_output_block`); the gates are code and apply to it as to any turn. A woken turn
   is never a nudge into a running one: the registry refuses it (`THREAD_BUSY`) and the waker
   waits that turn out. An answer to a ticket whose asking turn has ended starts a turn in the
   room it asked in, as the person's message (`answer_after_turn`); the ask cards follow it. That
   room is mapped as a delivery is (`wakeup_delivery_room`): a question a fired wake-up asked is
   answered where that wake-up reports, never on its hidden lane.
5. **Bounded with nobody watching.** A plan carries itself on (auto-advance; a woken turn may build
   the next phase whose dependencies landed). Rebuilding the SAME phase after it landed without
   completing is a repair: automatic repairs per phase are bounded by the person's limit
   `build.auto_repairs` (default 2, `runtime/user_limits.py`), then the build door refuses it from a turn nobody is in and the
   Host asks in the question box with the findings. Any other chain of woken turns is bounded per
   thread since the person last wrote (the person's limit `thread.woken_turns`, default 40, 0 for
   no limit; a plan's phase landing and the
   continuation of a research round that brought at least one result back are the plan advancing
   and do not count; a round where every agent failed does count; the bound is read when the
   woken turn starts): past it the turn still reports, and starts no work — the next step goes in
   the question box. Research rounds have their own bound, the person's limit
   `thread.research_rounds` (default 15) since the person last wrote, counting EVERY round
   whatever its agents' status; the continuation after the last starts no work and puts what
   came back in the question box. These bounds are the high backstop; what steers first is the
   outcome fed back: a round continuation's brief says which research round it was, names the
   agents that failed (their errors fenced as data in their reports) and, when nothing came back,
   how many turns have carried on by themselves so far. After `thread.failed_wakes` failed runs in a row in one thread (default 3), a
   failure is only written.
6. **Never lost.** A result handed to a turn that ends before reading it goes back through the one
   delivery door (`AgentDispatch.redeliver_unread`: released, re-delivered under the thread's
   lock, delivered only once written); a turn is woken for it unless the end was the person's Stop
   (the turn ceiling or a shutdown is not a Stop). The inbox row and the toast go out however the
   result arrived.
7. **Lanes nobody reads.** A replay, an A/B variant or an eval turn (`session.unseen_lane`) runs
   reads only — every other call is refused at one seat in the tool loop — and never holds, wakes
   or meets the go-ahead guard: its reply is the artifact being compared.
8. **Monitor → no orphans.** A background monitor loop scans the registry for agents that are
   completed-but-undelivered (deliver them — covers a dropped in-process handler) or in_progress past
   a timeout (mark stuck / follow up). Every dispatched agent reaches a terminal, delivered state.
9. **Waiting on a server: a watched call.** A Host that waits on someone else
   through a connected MCP server (its turn in a game on a game server, a job finishing) starts
   a watch instead of calling in a loop: `operate(intent, tool='mcp_connections', args={op:
   'watch', tool: 'mcp:<server>/<tool>', args, while_text})` (`runtime/mcp_watch.py`). The first
   call runs in the turn through every gate; an answer without `while_text` is simply its result.
   Otherwise the kernel repeats exactly that call with no model (an `mcp_watch` task, through
   `direct_ops.run_mcp_tool`'s gates every time, a call at most every `EVERY_S`), and the answer
   that differs comes back like a build's: into the holding turn, else a woken turn in the thread
   (`_deliver_watch_result`), with no row, inbox note or toast per answer. A watch's wake is the
   work advancing (`chain=False`); its own bound is the person's limit `thread.watch_wakes` (default 500) per
   thread since the person last wrote. A refusal ends a watch at once, `TRANSIENT_MAX`
   failures in a row end it, `RENEW_S` (a day) of waiting ends it, and the boot reap and the
   monitor cover it like an agent; `op='unwatch'` and the person's Kill stop it.

Mechanism: `AgentDispatch` holds a back-reference to the runtime; on completion `_run_and_record`
invokes `runtime.deliver_agent_result(...)`; the monitor loop is the safety net behind it. A
`delivered_at` marker on the record prevents double-delivery. `deliver_agent_result` makes no
model call: the fixed deliveries (a proof, a compare's candidates, a verdict, a research-round
pointer) are platform sentences, and everything the Host acts on goes to the Host.

### The go-ahead guard and how it is measured
The guard (`runtime/go_ahead_guard.py`) reads a reply's ending with a deterministic rule. It
is a high-recall PREFILTER, and the Host makes the call on the quoted sentence — within the rule's LEVELS, which nest: `core` ⊂ `explicit` ⊂ any
ask. CORE is the named pattern: a word to say, type or reply asked of the user, an option to
reply with, a direct question offering or awaiting the Host's step ("Want me to …?", "Should I
…?"). EXPLICIT adds the other explicit forms: a reply request with the Host's step on it, the
user's readiness gating the next step, input the Host needs. Everything else the rule flags is
JUDGED (an offer, a decision handed over, a condition). A core or explicit form, and a close_turn
handoff that records a wait, are asks by their shape: the correction offers only the work or the
question box (`escalate`). A judged reading may be no ask at all, so it alone gets a third answer:
end the turn adding nothing (an empty continuation or a close_turn with no text ends it with the
reply intact). A handoff set aside as a wait is never put back. It repairs at most once a turn,
plus once more each time a hold resumes and once at an exit past the loop. At every exit that ends
the turn on the Host's own prose and can still run a repair (`session._go_ahead_round`) it reads
every piece since the person last spoke that it has not judged yet (below): the text exit
— a terminal close_turn reaches it, and so does the reply after a close_turn that came beside
other calls, read together with the prose sent with that close_turn — the narrate-first dispatch
exit (the round's prose and its narration), the consecutive-failure breaker's answer and a reply
that arrives as the round backstop is reached (both leave through the text exit), and a result
that reaches the round backstop with prose beside its calls (`backstop`: the forced text is
skipped, so the turn ends on that prose). Past the loop, the forced text after the
Host closed the turn (`closed`) or when a result reaches the round backstop asking for tools with no
prose (`backstop`), and the retry after a reply that was empty once its Working Memory block was
stripped (`retry`), are calls that carry `escalate` alone — the one call that runs there, told so;
any other call they make is never run — and the guard reads what they end on with a correction that
offers only the question box or a rewrite that says what is done and what is left, once, and only
while the turn allowance has room for that repair call. While a CARD this turn filed is open in
front of the person — an `escalate` ticket, a platform tool's card (`pipeline`, `mgmt_run`,
`context_ingest`, `turn_type`) or a phase's approval card raised by a refused `build`, read the
way the client raises them (`go_ahead_guard.card_from_tool`, mirroring `app-web-kit/ask.ts`) —
the card holds the question, so a closing line in prose that asks is never needed: every reading
fires, question or statement, at any level, and the correction names the card ("Your question is
already in the question box (ticket #N). End your reply without asking it again; say what you
will do once they answer, naming the step…") instead of sending the Host to `escalate` again. It
describes the ending's shape — what their answer starts — and never gives a sentence to copy; a
different question is asked once this one is answered (no tool adds to an open decision ticket).
A card closes only on an answer that names its ticket ("Ticket #41 — I chose …"). The guard
repairs once per turn-ending completion: once, and again each time a hold resumes on outside
input and the turn writes a new ending; the repair's own answer is never repaired again. A handoff
wait after an ask in the box is let through only when the box, its card or the escalation is the
thing awaited ("… on the card", not "… on the card design"). The exits that end without the
guard make no reply of the Host's to read — a model error (what was already sent is stored first,
below) — or
are the turn allowance's answer, written with no tools: when the allowance stops a turn, the platform
first raises its own continue card in the question box (`escalate` with `source: turn_allowance`,
options Continue / Stop here; either answer starts a turn of its own, with a budget of its own, so
"Stop here" tells the Host to leave the rest), and the steering tells the model the card is there,
so its answer reports what is done and what is left and never asks. While a card the turn filed is
still open the stop raises no second card — the answer to that one already starts the follow-up —
and the steering only asks for a report. Every one of these rows is still measured (a model
error's and a Stop's rows carry `metadata.go_ahead` too, below). The worst-case overspend of the
allowance is unchanged by any of this: after the projection says "stop", at most the stop's answer
and the empty-reply retry run (two calls, neither cut mid-stream); the exits' repair call starts only
when the allowance's projection still has room for it, and the continue card is no model call.

Every Host turn's row carries `metadata.go_ahead`: three readings of the reply by the detector —
`stored_asks` / `stored_level` (the STORED reply, every piece, never cleared), `final_asks` / `final_level` (every ask since the
person last spoke that is still unresolved, each judged on its own — below) and
`ending_asks` / `ending_level` (the last piece of the Host's that has text) — plus `asked_in_box`
and `fired` —
one entry per firing with the `rule`, the quoted `sentence`, `where` (reply or handoff), `exit`
(text, dispatch, backstop, closed or retry), `level` (core, explicit or judged; null for a handoff wait, which has no rule
level), `open_card` (the card that already held the question, or null) and the `outcome`:
`work_started` (`task_ids` / `ops`), `escalated` (`ticket_id`), `not_an_ask` (judged only),
`unrepaired` (a core, explicit or handoff ask the repair added nothing to), `rewrote`,
`repair_failed` (the repair call errored) or `superseded`. `final_*` counts, among the pieces
since the person last spoke: an ask in any piece the guard has not judged, or in a
repair's own text (the guard never reads that); every firing whose repair resolved nothing — a
repair resolves its ask by `work_started`, `escalated`, `not_an_ask`, or a rewrite that stops
asking while a card holds the question; and the ending, when the guard fired on it and it still
asks (a judged reading the Host let stand). The surest of them is recorded (core, then explicit,
then judged). The person's words answer every piece SENT before them — one order numbers every
word pushed into the turn and every piece it sends (`session.send_order`), so an ask that went out
after their words, while those words sat in the queue, is not answered by them; a
result answers nothing. An ask the repair only added to stays counted in all three (a "Say go" with a sentence
after it still reads core), and so does an ask the guard fired on whose repair resolved nothing,
however many rounds or results came after it — a repair that reads something first and then
writes a plan reads the same as one that writes the plan at once — and an ask the
guard could not read yet (its repair spent, input queued) is counted until it does. At the
round backstop and the exits past the loop nothing will read queued input, so there the guard
reads anyway (queued words still answer what was sent before them). So
`stored_*` answers "does what the person reads still ask?", `final_*` "is an ask since the person
last spoke still unresolved?", and `ending_*` "what did the Host's last text end on?"; a turn whose
repair started the work reads core in `stored_*` and null in `final_*`, and so does a turn whose
only ask was narration beside a tool call ("Should I check the logs first? Checking now." — the
guard never judges it, so it is never counted as a failure in the daily `still_*` columns) —
unless the turn ends on that narration, nothing of the Host's after it.

What the stored reply is, exactly — ONE recorder (`turn_replies.TurnReplies._send`): every piece
of visible text the turn sends — each model completion's text, the dispatch narration, the stall
notice — is recorded exactly once, at the one point where it is sent to the stream, and the
stored reply is those pieces in order, joined with `session.REPLY_SEP` ("\n\n"). The stream sends
the same separator before a piece's first text when text already reached the person this turn
(`brain_service` puts it before the dispatch narration frame). So for EVERY turn the text the
person sees live, the stored row, the reply `send_message` returns, and what the end-to-end test
reads by concatenating the `0:` frames are one string by construction: nothing else adds a piece
or rebuilds the reply from fragments, and every piece is kept ("Done", then "Done — all three
checks pass." are two pieces). Text that is never sent is never recorded. A completion that opens
with a parroted Working Memory block is held back as it streams; when it ends, what
`session.strip_wm` leaves of it is sent, and so recorded. A `<skill_events>` block (the Host's
notes on its own skills) is held out of the stream as it arrives and handed whole to the apply
step; it used to stream live and be cut from the row afterwards. Reasoning a model writes in-band
(`<think>` / `<thinking>`) where it OPENS a completion — nothing but blank space before it — is
held out the same way and dropped, closed or not, as `provider_gateway.strip_thinking` reads an
opening tag: never shown, stored or read by the guard. The same tags after the first visible text
are the Host writing about them, and are sent as written. Reasoning with no opening
tag, only a closing `</think>`, cannot be seen starting here: a route flagged `reasoning_inline`
(set on a stored route through the Orchestrator's routing write, `PUT /api/inference/routing`,
or on a Circles thread override's model pick; off unless set — the app has no control for it yet) has its stream held
back by the OpenAI-wire adapter until the close (`provider_gateway._consume_openrouter_stream`)
— only while the reasoning is inline: a provider that returns it in its own field (Groq parsed,
or any delta carrying `reasoning` / `reasoning_details`) streams the reply as it comes — and reasoning is asked for separately where the provider documents a way (OpenRouter's
`reasoning.exclude: false`; Groq's `reasoning_format: "parsed"` on a flagged route). A turn that
ends before writing its own row still stores what the
person read: from its first visible text the turn's trace carries the recorder's pieces
(`session.note_turn_reply`), so a Stop outside a hold, or a model error mid-turn, writes them as
the Host's reply (`session.keep_sent_reply`; after a hold that resumed, the held row is rewritten
in place) before the stopped-turn note — with the go-ahead record of exactly what it stores (a
held row's metadata is merged on rewrite, so a record taken at the hold would otherwise stay).

Each piece gets ONE label, when its completion ends: `narration` — prose in a tool round that goes
on to another model round (the person read it; the guard does not judge it); `reply` — closing
text: a reply, the prose sent with close_turn whatever rode beside it (close_turn tells the Host
its next call only adds to that text), a dispatch round's prose and its narration, the prose a
result reaches the round backstop with; `repair` — a guard's repair (the go-ahead guard's or the
claim guard's). What the go-ahead guard reads is decided per piece, by ONE rule: the boundary moves only when the
turn reads the PERSON's words — an answer to the box, or text they typed into the turn — and a tool
or agent result never moves it, because a result is not the person's answer. Every visible piece
is judged at most once (`clean`, `fired`, a repair's own answer, or the platform's own line, never
judged). At every exit the guard reads every piece since the person last spoke that it has not
judged — narration excluded unless the turn ends on it — and judges them all; where it cannot read
(its repair spent, input queued) it judges nothing, and the next exit reads them. So a question
the person has since answered is never repaired, an ask written before a result came back is
read with what follows it, and while its one repair is unspent (one per turn, never re-armed)
the guard reads everything it has not judged. A
repair's own text is held until its round's calls have run and may claim the question box only
with a card open (`go_ahead_guard.repair_text`); a repair answered with close_turn alone ends the
turn, and once a reply to the latest input is on screen no exit asks for "your reply" again.
`final_*` counts what is still unjudged, so the guard and the record never read different
text. The labels are for the ledger only. The thread IS
the Host's chain of thought: nothing the person read is carried out of sight,
so narration, which once streamed live and was lost on reload, is stored too (a round whose tool
calls the person's words dropped keeps its prose). `context_ledger.reply` records the stored
reply's length, each piece in order as `[label, chars]` (`pieces`), and the narration in it
(`tool_prose_parts`, `tool_prose_chars` — close_turn's own prose is a reply, not counted
there), so its weight in the row and in the history later turns replay is measured from rows:

```sql
-- per day: tool turns, and the narration (prose beside tool calls) their stored replies carry
SELECT date_trunc('day', created_at) AS day,
       count(*) AS tool_turns,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (metadata->'context_ledger'->'reply'->>'tool_prose_chars')::int)
         AS median_tool_prose_chars,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (metadata->'context_ledger'->'reply'->>'chars')::int)
         AS median_reply_chars
  FROM entity_memory
 WHERE memory_type = 'turn' AND metadata->>'role' = 'assistant'
   AND metadata ? 'tool_calls' AND metadata->'context_ledger' ? 'reply'
 GROUP BY 1 ORDER BY 1;
```

The record is
written on the held row too, before a hold, so a Stop, the
turn ceiling or a restart mid-hold leaves the record so far. The success measure, from rows —
the final text's ask is counted whatever the outcome, split by level the way the sets nest
(`still_explicit` counts the core asks too), each turn judged on its LAST firing, `not_an_ask` its
own column:

```sql
-- per day: Host replies, replies whose FINAL text still asks (whatever the
-- outcome) — any ask, an explicit form (core included), a core form — how many
-- the guard fired on, and how many ended on "not an ask"
SELECT date_trunc('day', created_at) AS day,
       count(*) AS replies,
       count(*) FILTER (WHERE metadata->'go_ahead'->>'final_asks' IS NOT NULL) AS still_asks,
       count(*) FILTER (WHERE metadata->'go_ahead'->>'final_level' IN ('core', 'explicit')) AS still_explicit,
       count(*) FILTER (WHERE metadata->'go_ahead'->>'final_level' = 'core') AS still_core,
       count(*) FILTER (WHERE jsonb_array_length(COALESCE(metadata->'go_ahead'->'fired', '[]')) > 0) AS guard_fired,
       count(*) FILTER (WHERE metadata->'go_ahead'->'fired'->-1->>'outcome' = 'not_an_ask') AS last_not_an_ask
  FROM entity_memory
 WHERE memory_type = 'turn' AND metadata->>'role' = 'assistant' AND metadata ? 'go_ahead'
   AND created_at > now() - interval '30 days'
 GROUP BY 1 ORDER BY 1;

-- turns the guard fired on, by their LAST firing's outcome and level (a
-- handoff wait shows as 'handoff'), with how many of each still end asking
SELECT metadata->'go_ahead'->'fired'->-1->>'outcome' AS last_outcome,
       COALESCE(metadata->'go_ahead'->'fired'->-1->>'level',
                metadata->'go_ahead'->'fired'->-1->>'where') AS last_level,
       count(*) AS turns,
       count(*) FILTER (WHERE metadata->'go_ahead'->>'final_asks' IS NOT NULL) AS still_asks
  FROM entity_memory
 WHERE memory_type = 'turn' AND metadata->>'role' = 'assistant'
   AND jsonb_array_length(COALESCE(metadata->'go_ahead'->'fired', '[]')) > 0
   AND created_at > now() - interval '30 days'
 GROUP BY 1, 2 ORDER BY 3 DESC;
```

The rows live in each Host's own database. In PII mode the row's metadata keeps structural
keys only, so `go_ahead` (it quotes the Host's sentence) is not kept there.

## Governance after the veto
Structural only: input_guard (screen) + CEIGAS (trust/math gate) + read-only grants on dispatched agents,
and the crisis check's notice beside the turn, never in it (step 3 above).
No LLM judging an LLM. Matches "govern by math, not surveillance."
