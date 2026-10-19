---
name: codex
description: How to use the Codex CLI harness (OpenAI's official agent runtime) when it's installed. Subscription-passthrough against the user's ChatGPT Plus/Pro/Business/Enterprise/Edu plan. Different perspective from the Claude Code harness — useful for cross-vendor comparison.
present_if_app_installed: codex
allowed-tools: build, review.compare, coding_bench, harness_connections, review.adopt
---

# Codex SOP

The Codex CLI (OpenAI's official Rust agent, `github/openai/codex`) is
one of several coding harnesses available. This skill is only present
on hosts that have the `codex` app installed — meaning the user
wants Codex specifically for some part of their coding work.

## When to use Codex

**Use it for:**
- Coding tasks where you want OpenAI's specific perspective (different
  training data, different system prompt assumptions, different
  tool-use behavior than Claude)
- Multi-harness comparison — fire the same task against Codex alongside
  Claude and Cursor as a compare; the user picks
- Long-running coding sessions when the user's Claude subscription
  is rate-limited (Codex draws against a separate budget — ChatGPT)

**Don't use it for:**
- Plain conversational chat (use the chat-role model via model_router)
- Tasks the user explicitly steered to a different harness
- Anything when the user's ChatGPT subscription token is expired —
  surface for re-auth on the user's own login surface

## Models and effort

The Codex CLI serves its OWN model catalog —
`harness_connections(op='matrix', harness='codex')` lists it on the codex
row (`models` with `models_source: cli_catalog`, each
model's accepted `efforts` and `default_effort`, and `default_model`: what a
build with no model named actually runs). Pick from that list only — a model
id from another provider's catalog is not one this CLI can serve, and an
effort the catalog does not list for the chosen model is dropped at the pod.
Never quote a model or effort from memory: the pinned CLI version decides,
and it moves when the harness image is updated.

## How it connects

The platform ships no sign-in of its own. Two ways the grant lands at
`/home/privatae/.codex/credentials.json` (the Codex CLI picks it up
automatically — no env var injection needed):

- **Connect through you** — `operate(tool='harness_connections', args={op:'login',
  provider:'codex'})` runs OpenAI's OWN `codex login --device-auth`
  inside the user's codex pod and returns `{url, user_code}`. Give the
  user BOTH: they open the link in their own browser, sign in, and type
  the one-time code on that page. When they say they have, call
  `op='login_status'` — that reads the grant out of the pod and saves it
  to this host. Nothing is pasted into Keys. The code and the pod both
  expire in about 15 minutes.
- **Paste in Keys** — a user who already has a codex OAuth JSON (id_token /
  access_token / refresh_token) puts it in the Keys app under `harness-codex`.

## How to invoke (tools)

Codex runs on the harness pool, reached only through your own build
tools (there is no `codex.run` tool):

- `build(action='build', project_id, harness='codex')` — one approved
  phase on Codex (plan → user's yes → build; see build-workflow.md).
- `build(action='run', project_id, prompt, harness='codex', model?)` — a
  one-off build with no plan; `build(action='run', prompt, variants=[{harness:
  'codex', model: …}, …])` — Codex alongside other engines, or several Codex
  models side by side, in a compare (variants of codex share its pod).

- `coding_bench()` — the host's coding-capability read: connected
  harnesses (incl. whether Codex is connected and its binary state),
  routing, and per-model track record. Call this before building on
  Codex if you're not sure the subscription is connected; saves
  a failed attempt + a token spend. (`codex.status`/`codex.sessions`
  never existed as callable tools — removed 2026-07-12.)

## Costs

Codex calls bill against the user's ChatGPT subscription (the OAuth
token IS the subscription). Privatae Host doesn't add a per-call charge;
its side is the harness's compute time, recorded for the monthly bill.

If the user is on the BYOK path (OPENAI_API_KEY instead of OAuth),
each call bills their OpenAI account directly.

## Multi-harness pattern

When the user has Codex + Claude + Cursor installed, the host can
run a single coding task as a compare via `build(action='run',
variants=[…])`:

1. Each variant runs in its own git worktree
2. Each produces a diff (and a check verdict when `acceptance` was given)
3. `review.compare` shows the candidates side by side — no model picks
4. The user picks; `review.adopt(session_id, harness=<pick>)` lands it

This is where Codex earns its keep — it's the "different perspective"
the user can set beside Claude's instinct.

## The binary

The Codex CLI is baked into the harness image at a pinned version (the
image tag is the binary version); nothing is downloaded at run time.
`coding_bench` shows whether the harness is connected.

## When to refuse

- User's ChatGPT subscription token expired → surface for re-auth on the
  user's own login surface, don't retry
- User asks Codex to do something a cheaper model could handle
  (single-line completion, syntax question) → use chat-role model
  instead. Subscription quota matters.
