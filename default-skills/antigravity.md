---
name: antigravity
description: How to use the Antigravity CLI harness (Google's agentic coding CLI, `agy`) when it's connected. Runs on the user's own Google account — the Google AI Pro / Ultra plan they pay for — through the vendor's own sign-in, or on a Gemini API key they paste in Keys. Google's perspective beside Claude, Codex and the others.
present_if_app_installed: always
allowed-tools: build, review.compare, coding_bench, harness_connections
---

# Antigravity CLI SOP

Antigravity CLI (`agy`, antigravity.google) is Google's coding agent and the
vendor tool for Google AI Pro / Ultra accounts — Gemini CLI stopped serving
those sign-ins on 2026-06-18, so "use Gemini / use Google" for agent-shaped
coding work means this harness. Harness id: `antigravity`. Like every coding
harness it runs in its own confined pod on the harness pool; you reach it only
through your own build tools. Availability is CONNECTION, not a dock app
(there is no App Store row for it, as for Kimi Code): `coding_bench` /
`harness_connections op='matrix'` say whether it is connected; this SOP is
always present so you know how to connect it.

## When to use it

**Use it for:**
- Coding tasks where the user wants Google's perspective (Gemini models; the
  vendor's own catalog also lists some Claude models — `agy models` decides,
  not this file)
- Multi-harness comparison — the same task on Antigravity beside Claude Code,
  Codex or Kimi Code; the user picks the best per file
- Work the user wants billed to their Google plan rather than another vendor's

**Don't use it for:**
- Plain conversational chat (the chat-role model via the model router)
- Tasks the user explicitly steered to a different harness
- Anything while the user is not connected — connect first (below); never
  retry a build into a 401

## Models and effort

The binary serves its OWN model catalog (`agy models`, slugs like
`gemini-3.7-flash-high`, `gemini-3.1-pro-high`). The coding matrix
(`harness_connections(op='matrix', harness='antigravity')`) shows what
is known for the row; when nothing is listed, omit `model` and the binary
runs its default. Never quote a model slug from memory — the pinned CLI
version decides, and it moves when the harness image is updated.

Effort is `low | medium | high` (the binary's own `--effort` flag). Anything
else is dropped at the pod, so offer only those three.

## How it connects

The platform ships no sign-in of its own. Two lanes:

- **Connect through you (the user's Google account — primary).**
  `operate(tool='harness_connections', args={op:'login', provider:'antigravity'})` runs Google's OWN
  sign-in inside the user's Antigravity pod and returns `{url, needs_code:
  true}`. Hand the user the link AT ONCE: they open it in their own browser,
  sign in with the Google account their plan is on, and Google's page then
  shows them a code. Take that code from them and pass it straight through
  with `op='login_code'` (provider + code) — you are relaying it, not storing
  it; nothing goes to the Keys app. The grant the binary writes is saved to
  this host by that call. **The binary waits about 60 seconds for the code**;
  if the user was slower than that the relay reports the login exited — just
  run `op='login'` again for a fresh link, it costs nothing. The pod stays up
  while a login is waiting and shuts itself down within ~15 minutes.
- **Gemini API key (pay-per-token).** A user who prefers to bill Google's API
  directly stores a Gemini API key (Google AI Studio) in the Keys app as
  `harness-antigravity`. With no Google sign-in present the harness runs on
  the key; when both exist the sign-in wins.

Reconnect = the same login flow again. The matrix cannot tell a dead Google
grant from a live one (the binary refreshes its own token; the row reads
presence), so a revoked grant shows up as a FAILED RUN — the sidecar returns
"authentication failed or timed out" and the run is not retried. That is the
signal to run the login again. A stored Google grant always wins over a pasted
key: to move a user onto the key lane, the grant has to go (the login replaces
it; there is no "forget" button — tell the user plainly).

## How to invoke (tools)

- `build(action='build'|'continue', project_id, harness='antigravity',
  model?, effort?)` — one approved phase on Antigravity (plan → user's yes →
  build; see build-workflow.md). This is the lane for "use Antigravity /
  Gemini for this".
- `build(action='run', project_id, prompt, harness='antigravity', model?)` — a
  one-off build with no plan; `build(action='run', prompt, variants=[{harness:
  'antigravity', model?}, …])` — Antigravity alongside other engines in a
  compare (the user picks; see review.md).
- `coding_bench()` — the host's coding-capability read: connected harnesses
  (whether Antigravity is connected and its binary state), routing, track
  record. Check it before building if you are not sure the account is
  connected; it saves a failed attempt.

## Costs

Signed-in runs draw on the user's Google AI plan quota (Pro / Ultra; the plan
decides the rate limit and refresh). Key runs bill the user's Google API
account per token. C-Series adds no per-call charge, and Privatae bills
nothing for the run.

## A vendor quirk you will see in the run feed

`write_to_file … is not a valid artifact path; artifacts must be in
…/brain/<conversation-id>/` — this is Antigravity's OWN rule: `write_to_file`
carrying artifact metadata (plans, walkthroughs — "artifacts") may only
target its per-conversation brain directory. The agent hits it when it writes
a project file as if it were an artifact; it recovers on the next step with
`replace_file_content` (measured on the first live build, 2026-08-21: the
app landed and rendered). It is NOT a platform or permissions error and needs
no action from you — the run's verdict is the exit code and the delivered
files, not that line. The build prompt already tells the agent to write
project files plainly; if the line keeps recurring across builds, say so.

## When to refuse

- Not connected / expired → connect or reconnect first, don't retry
- The user asks Antigravity to do something a cheaper model could handle
  (single-line completion, syntax question) → use the chat-role model
  instead; plan quota matters
