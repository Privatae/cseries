---
name: claude-code
description: How to use the Claude Code harness when it's installed. Anthropic's official agent runtime — multi-turn tool loop, code editing, sandboxed Bash — running as a coding harness on the harness pool. Subscription-passthrough to the user's Claude Pro/Max plan.
present_if_app_installed: claude-code
allowed-tools: build, review.compare, coding_bench, harness_connections, review.adopt
---

# Claude Code harness SOP

The Claude Code harness (harness id `claude-code`) is one of several coding harnesses the user may have
installed. This file is only present on hosts that have the
`claude-code` app installed — meaning the user wants Claude
specifically for agent-shaped work. Like every coding harness, it runs
in its own confined pod on the harness pool — code never executes inside
the host container. You reach it only through your own build tools:
`build(action='run'|'build'|'continue', harness='claude-code', model='claude-opus-5')` for a
single run (the `model` argument is how "use Opus 5" is honoured — the
harness defaults to claude-opus-5 when you omit it), or
`build(action='run', variants=[{harness: 'claude-code', model?}, …])` when it
is one of SEVERAL engines taking the same task (a compare).

## When to use this harness

**Use it for:**
- Multi-step code changes (read → edit → test → commit)
- Anything that needs the tool loop (Bash, Read, Write, Edit, Glob, Grep)

**Don't use it for:**
- Plain conversational chat — that goes through the host's chat
  model via the model router (typically a cheaper model like
  Haiku). Burning Opus tokens on chitchat is waste.
- Pure-classification tasks (sentiment, intent detection) — the host
  model does those in its own turn
- Embedding generation — that goes through the bridge to the embedding
  service (per the embedding skill)

## How it connects

Run Anthropic's own login for the user:
`operate(tool='harness_connections', args={op:'login', provider:'claude-code'})`
returns `{url, needs_code}`. Hand them the link at once; they sign in on
claude.com, copy the code the page shows, and you relay it verbatim with
op='login_code' (skill 'coding-protocols'). Nothing goes to Keys. A BYOK user
may instead put an Anthropic API key in Keys. The harness pod picks the
credential up at wake; refresh is automatic. If a 401 surfaces during a run,
the runtime drops an inbox notification asking the user to reconnect.

## Tools available when this skill is loaded

- `build(action='plan'|'build'|'continue', project_id, …)` — the ONLY
  door to building, and it enforces plan → build:
  `action='plan'` composes a research-grounded phased plan (SHOW it as
  the build starts — the user's request is the yes); `action='build'` runs
  the ready phases on the harness pool (refuses plan-less projects, returns a
  task_id immediately, runs in the background — you're woken with the
  outcome); `action='continue'` is a follow-up seeded rerun against the
  project's current state. See build-workflow.md for the full protocol.
- `build(action='run', variants=[...])` / `coding_bench` — the same task on
  several engines, compared; see review.md and benchmark.md.

Web research is NOT this harness's job and never was routed through it:
the `research` and `web_search` tools run on the Orchestrator's
host model with that provider's own web search (see
research.md). If research fails, the failure names the provider and model
that failed — relay that; it has nothing to do with Claude Code.

## Costs

Inference on these runs rides the user's Claude subscription (Pro/Max)
— Privatae Host doesn't add a per-token charge. The pool pod's compute window
(wake → release) is metered and billed as compute, the same as every
harness run — that is the platform's compute-only billing model. If
the user is on the BYOK path (API key not subscription), inference
bills against their Anthropic account directly.

## When the user has MULTIPLE harnesses installed

If the user has both Claude Code AND Codex/Cursor installed,
the multi-perspective pattern is available:

1. The host runs the **same task** on each named engine at once
   (`build(action='run', variants=[…])`, each variant in its own git
   worktree)
2. `review.compare` shows the candidates side by side — engine, model,
   checks, files, preview; no model judges
3. The user picks; `review.adopt(session_id, harness=<pick>)` lands it

This pattern is invoked deliberately — not on every step — when the user
wants cross-vendor perspective. See review.md. For the normal single-harness,
owned, phased build, use `build`.

## When to refuse

- User asks the harness to embed text. That's the bridge's embedding
  service, not the harness. Hand off.
- User's claude-code subscription is expired (status=invalid). Don't
  retry the build; run the same login again (`harness_connections`
  op='login') and hand them the link.
