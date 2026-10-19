---
name: coding-protocols
description: How to follow a coding protocol — the user's JSON instruction file from Orchestrator → Multi-Agent Coding — step by step with your own build tools; how to write one with the user; and how to record the outcome so the list shows what works. A protocol is instructions you read, never a program the platform runs.
allowed-tools: coding_protocol, coding_bench, harness_connections, build, review.compare, review.adopt, review.propose_commit, ladder.run, ladder.status, harness_costs, harness_transcript
---

# Coding protocols SOP

The Orchestrator has two coding sections. **Coding Engines** is one row per
engine (Claude Code, Codex, Cursor, Grok, opencode, Kimi Code, Antigravity
CLI): connected or not, its default model, its default effort, and ONE
"Default" tick — an unnamed `build` runs there. **Multi-Agent Coding** is a
list of protocol files. A protocol is a JSON file of INSTRUCTIONS the user
(or you, with the user) wrote: which engines to run, one after another or
several at once, with which model and effort, a time budget if any, and what
to do on pass and on fail. You follow it with the tools you already hold.

Nothing here runs by itself. You read the file, you run each step through
`build` / `ladder.run`, you ask the user, you record the
outcome. The platform stores, validates, preflights and measures — it never
drives.

## The file

```json
{
  "format": "privatae.coding-protocol/v1",
  "name": "Cheap first, then the bench",
  "author": "alex",
  "description": "one line, optional",
  "notes": "anything the host should know before starting, optional",
  "steps": [
    { "id": "kimi-first",
      "engines": [ { "engine": "kimi" } ],
      "on_pass": "end", "on_fail": "bench",
      "notes": "run the project's tests before calling it a pass" },
    { "id": "bench",
      "engines": [ { "engine": "claude-code", "model": "claude-opus-5", "effort": "high" },
                   { "engine": "opencode" } ],
      "samples": { "claude-code": 2 },
      "time_budget_minutes": 20,
      "on_pass": "end", "on_fail": "end" }
  ]
}
```

- One engine in a step = a single build: `build(action='run'|'build', harness=<engine>,
  model=<model?>, effort=<effort?>)` — the effort is checked against that
  engine's own set (the file was validated against the same list, so a saved
  step's effort is one its engine takes).
- Several engines in a step = a compare, they run AT ONCE:
  `build(action='run', variants=[{harness, model?, effort?}, …],
  max_minutes=<minutes of silence>)` — one entry per engine, and a step's
  `samples: {engine: N}` means that engine's entry N times. Then
  `review.compare` shows the candidates, the user picks, `review.adopt` lands
  the pick.
- Steps chained by `on_fail` ARE the ladder. If a chain of single-engine steps
  should climb on the project's own tests, `ladder.run(rungs=[…],
  test_cmd=…)` walks it for you — one call, verified climb. Translate ONE
  rung per step: a step `{"engines": [{"engine": "kimi", "model": "k3",
  "effort": "high"}]}` becomes the rung `{"harness": "kimi", "model": "k3",
  "effort": "high"}` (add `samples` from the step if it has one); an at-once
  step at the end can be `{"harness": "*"}` only if it means the whole bench.
  A step passed verbatim (`engines: […]`) is refused by the ladder.
- `on_pass` / `on_fail` name the next step id, or `"end"`. Defaults: on_pass →
  end; on_fail → the next step in order, else end.
- `time_budget_minutes` is optional ("if any"). It is not a limit on how long
  a step may work: it becomes the step's `max_minutes`, the longest one model
  call may go with nothing arriving before the run is stopped as hung. The
  ladder also stops waiting on a step after that many minutes plus three.
- `auth` on an engine is filled in for you: `"oauth"` (the vendor's own login)
  or `"api_key"` (a key in Keys). It names the KIND of grant — a protocol never
  carries a credential: never put one in, and the validator refuses a file
  that carries a recognisable one (a secret-named field, a known key shape).
- `author` is credit. When a file is shared its stats can ride along; the
  receiving host's preflight says whether ITS engines are granted.

## Following one

1. `coding_bench` — it lists the protocols and the engines (and the default).
   `coding_protocol op='get' slug=…` reads the file. Read the `notes`.
2. `coding_protocol op='status' slug=…` — the grant preflight. Every engine the
   file names must be connected HERE. If one isn't, that row's action (a
   vendor login through `harness_connections op='login'`, or a key in Keys)
   comes BEFORE running anything: start it when the user's request covers
   it, otherwise put it in the question box. Never substitute a different
   engine silently; never start a step whose engine isn't granted.
3. Run the first step with the matching tool (above). Say which engine(s) are
   building and why — one line. Builds and compares report back into the
   thread when they land; do not poll.
4. When it lands, tell the user what ran and ask for the verdict:
   - **pass** — the user says pass (or the step's notes say what counts, e.g.
     the tests passed): `coding_protocol op='record' slug=… run={result:'pass',
     steps:[…], session_ids:[…], task_ids:[…]}` and stop.
   - **fail** — the user says fail, or it obviously failed (the run errored,
     nothing landed): follow `on_fail`. Carry the failure EVIDENCE forward
     (error text, what the user disliked), never the failed diff. When the
     file says `end` after a fail, record `result:'fail'`.
   - The user stops early → record `result:'abandoned'`.
5. The session ids (a build's `session_id` is its billing key, e.g.
   `factory-build-292`; a compare's `session_id`) are what turn the record into
   billed truth on the list — always pass the ones you have.

## Writing one with the user

The user says what they want in plain words ("try kimi first, if it's bad
have claude and opencode both take a shot and I'll pick"); you turn it into
the file above and `coding_protocol op='save'` it (name + the document). The
validator refuses unknown engines, models an engine's key doesn't serve, an
effort an engine doesn't take, and any credential — relay its reason and fix
the file. Edits are `op='save' slug=…` with the changed document. The user can
also paste a file into the Orchestrator's Import box or hand-edit the JSON
there; you read whatever is stored.

## What a protocol is NOT

- Not a judge: pass/fail is the USER's say, or an obvious failure. No model
  scores another model's work.
- Not a default: an unnamed `build` runs on the Default engine tick, not on a
  protocol. A protocol runs when the user picks one, names one, or asks for
  the kind of work it describes.
- Not a place for secrets, and not something you invent engines for: only
  engines from `coding_bench`, only models their key serves.


## harness_connections — guidance

`op='matrix'` is the SAME build the Orchestrator's coding screen renders, so
use it to answer "which key is opencode running on?" or "why isn't Kimi Code
available?". It returns one line per harness: the key it runs on (`key`:
state, name, source, expiry), `provider`, `default_model`, the saved
`model_override` / `effort_override`, the `efforts` it takes, `model_count`,
and the row's own words when it cannot run (`models_error`, `models_loading`,
`action`). `harness_connections(op='matrix', harness='<id>')` returns that
one harness's whole row with its `models` (and each model's efforts where the
engine has them) — read it before you name a model for a build. `coding_bench`
carries each engine's model count the same way. `op='list'` returns every known harness provider with its
install state, connected state, credential validity, and expiry;
`op='status'` returns {present, valid, expires_at, auth_type} for one
provider. Either spelling of claude works: 'claude-sdk' (what op='list'
returns) and 'claude-code' (what the matrix returns) resolve to the same
harness.

**Login.** `op='login'` RUNS the vendor's own login command inside that
harness's pod and returns {url, user_code, done, needs_code} — the URL the
vendor printed, plus (device-code vendors, codex today) the ONE-TIME CODE the
user must type on that page. Hand the user the link AND the code when there
is one — the link alone is useless to them. What happens next depends on the
vendor, and the response tells you which:

- If `needs_code` is true (Claude and Antigravity today) the vendor's login is
  BLOCKED waiting for a code the user copies off the vendor's own page
  (Antigravity's Google page shows the code after sign-in; its wait is about
  60 seconds, so hand the link over at once and relay the code as soon as
  they have it — if it timed out, just run op='login' again for a fresh
  link) — take that code from them and pass it straight through with
  `op='login_code'` (provider + code, verbatim; never invent or reformat
  it); do NOT send them to the Keys app, you are relaying it, not storing it.
- Otherwise, for any harness whose sidecar can be polled (claude-code, codex,
  grok, antigravity), the vendor's CLI finishes by itself in the pod once the
  user has signed in — and typed the one-time code when the result carries a
  user_code (codex); grok's code is already inside its URL, so the absence of
  a user_code means nothing. When they say they are done, call
  `op='login_status'` to read the grant out of the pod and save it to this
  host; nothing goes to the Keys app, and the host is NOT connected until
  that call lands.
- If login_status answers supported=false for that harness, send them to the
  Keys app if the vendor handed them a key to paste.

**Grok can also be signed in from the Keys app.** The xAI row there has a
"Sign in with xAI" button ("Reconnect with xAI" once a sign-in is stored)
that runs this same grok login, checks it until the user finishes, and saves
the sign-in itself — and if the host had no chat route at all, routes chat to
an xAI model the Host model standard lists eligible and tells them which. It exists for
a user whose only subscription is Grok, who has no model and so no host to
ask. If they say they signed in from Keys, there is nothing for you to poll.
Do not run op='login' for grok while they are signing in there: a new login
replaces the one in flight in the pod.

The result's own `next_step` says which of these applies — follow it. The pod
STAYS UP while a login is waiting and shuts itself down within ~15 minutes,
so do not leave a user hanging. The platform never mints, stores or sees the
grant — the vendor's own binary does the whole exchange on the user's own
compute; op='matrix'/'list'/'status' write nothing at all.

## coding_protocol — guidance

A protocol is a JSON file of INSTRUCTIONS, not a program: steps in order,
each naming one engine (a single build) or several engines to run AT ONCE (a
compare you then let the user pick from), with an optional model/effort per
engine, an optional time budget, notes, and on_pass / on_fail targets ('end'
or another step). You follow it yourself: `op='status'` first (every engine
it names must be connected HERE — the row's action comes first if not,
never a substitute), then run each step through build / ladder.run,
tell the user what ran, and ask: PASS → `op='record' result='pass'` and stop;
FAIL (the user says so, or it obviously failed) → follow on_fail, and when
the run ends record its result (+ session_ids/task_ids of the harness runs so
the stats carry billed truth). `op='save'` writes a file the user and you
agreed (name + the document; a file never carries a credential — 'auth' says
only what kind of grant an engine needs). Engines are ids from coding_bench
(claude-code, codex, cursor, grok, opencode, kimi, antigravity); models must
be ones the engine's key serves. A protocol can be shared as-is — the author
field is credit.


## Parameter notes (full text of shortened argument descriptions)

- **coding_protocol** `op`: list = all protocols (+stats); get = one file; status = grant preflight; save = write (create, or replace with slug); delete; record = a run's outcome.
- **harness_connections** `op`: matrix | list (default) | status | login | login_code | login_status. login/login_code/login_status need 'provider'; login_code also needs 'code'.
- **harness_connections** `provider`: Provider id (claude-sdk / claude-code are the same harness; codex, cursor, grok, antigravity, …). Required for status, login, login_code, login_status.
- **harness_connections** `code`: For op='login_code': the code the user copied off the vendor's own login page, passed through verbatim. Never invent or reformat it.
- **coding_protocol** `slug`: The protocol's slug (from op='list'). Required for get/status/delete/record; for save it means replace that file.
