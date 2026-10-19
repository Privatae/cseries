---
name: cursor
description: How to use the Cursor headless CLI harness when it's installed. Cursor Pro subscription via User API Key — third-party perspective alongside Claude and Codex.
present_if_app_installed: cursor
allowed-tools: build, review.compare, coding_bench, harness_connections, review.adopt
---

# Cursor SOP

Cursor's official headless CLI (`cursor-agent`) is one of several
coding harnesses available. This skill is only present on hosts
that have the `cursor` app installed.

Cursor under the hood is mixed-model (Anthropic + OpenAI + their own
tunings), routed through Cursor's backend. Subscription path is
Cursor's, not Anthropic's or OpenAI's — users with a Cursor Pro
subscription have already paid for the tokens.

## When to use Cursor

**Use it for:**
- Coding tasks where the user wants their Cursor Pro subscription
  to do the work (zero marginal cost for them)
- Multi-harness comparison — fire the same task across Cursor
  alongside Claude and Codex as a compare; the user picks
- Tasks where the user has heavy daily usage and wants to spread
  load across multiple subscriptions

**Don't use it for:**
- Plain conversational chat — use the chat-role model
- Tasks the user explicitly steered to a different harness
- When the user's Cursor subscription has lapsed — surface it for
  re-keying on the user's own surface

## How it connects

The user authenticates on their own login surface (the platform ships
none). Unlike Claude Code / Codex (both OAuth-based),
Cursor uses a User API Key — the user pastes the key they generated at
cursor.com/settings into their own surface. Key lands at
`/home/privatae/.cursor/api_key` as JSON `{"api_key":"…","stored_at":…}`.
Sidecar injects it via `CURSOR_API_KEY` env on each run.

## Tools

Cursor runs on the harness pool, reached only through your own build
tools (there is no `cursor.run` tool):

- `build(action='build', project_id, harness='cursor')` — one approved
  phase on Cursor (plan → user's yes → build; see build-workflow.md).
- `build(action='run', project_id, prompt, harness='cursor')` — a one-off
  build with no plan; `build(action='run', prompt, variants=[{harness:
  'cursor'}, …])` — Cursor alongside other engines in a compare.
- `coding_bench()` / `harness_connections(op='status', provider='cursor')`
  — connection + key state before you build. Cursor has no quick verify
  endpoint — `valid` reflects "key present", not "key works";
  validate-on-first-use.

## Models — only what Cursor's own CLI lists

Cursor serves its OWN per-account catalog: `cursor-agent --list-models`
asks Cursor's backend what THIS subscription includes, and the CLI's
refusal names the same set ("Cannot use this model: X. Available models:
…"). So the ids look like Cursor's own (`composer-2.5-fast`,
`claude-opus-5-thinking-medium`) — the reasoning/speed variant is baked
into the id, which is why Cursor has no separate effort knob.

**Never pass a model id from another vendor's catalog.** An OpenRouter or
OpenAI slug (`gpt-5.6-luna-pro`, `openai/…`, `anthropic/…`) is not a
Cursor model and the run dies in seconds — that is exactly what happened
on 2026-08-21. Pick only from the ids the coding matrix shows for cursor
(`harness_connections(op='matrix', harness='cursor')`); they are read from the CLI in the user's own pod. If that list is empty
the matrix says so — then **omit `model` entirely** and Cursor runs its
own default, which always works. Do not guess an id to fill the gap.

## Costs

Calls bill against the user's Cursor Pro subscription. Privatae Host doesn't
add a per-call charge. Pod-uptime only.

## Multi-harness pattern

With Cursor + Claude + Codex all installed:

1. `build(action='run', variants=[{harness: 'cursor'}, {harness:
   'claude-code'}, {harness: 'codex'}])` runs the task on each engine in
   its own git worktree
2. Each produces a diff (and a check verdict when `acceptance` was given)
3. `review.compare` shows the candidates side by side; the USER picks
4. `review.adopt(session_id, harness=<pick>)` lands it

Cursor's value in this set: its model mix is different from the
other two harnesses, so when Claude/Codex converge on the
same approach Cursor's diff is often the contrarian signal worth a
second look.

## Binary bootstrap

The Cursor CLI binary is NOT in the host image. The sidecar
downloads it from `downloads.cursor.com/cli/<tag>/cursor-agent-linux-x86_64.tar.gz`
on first start. If bootstrap fails, `coding_bench()` shows the binary
state — surface it to the user.

## When to refuse

- API key invalid or subscription expired → re-key on the user's own
  surface, don't retry
- Binary failed to bootstrap → alert user, suggest network/firewall
  check
- User asks Cursor to do something a chat-role model could handle
  → don't burn subscription quota on it
