---
title: The Orchestrator app
summary: The Orchestrator decides which AI model does which job for your Host, on provider keys or sign-ins you hold, and which coding engine runs your builds. It is built into every Host and cannot be removed.
section: apps
order: 10
questions:
  - How do I choose which AI model my Host uses?
  - Can I use different models for different jobs?
  - What happens if my AI provider goes down?
  - Which coding tools can my Host build with?
  - Can I run my Host's memory search on my own machine?
  - Does Privatae charge me for the models I use?
app: orchestrator
---

The Orchestrator decides which AI model does which job for your Host, on provider keys or sign-ins you hold in the [Keys app](wiki:keys-app), and which coding engine runs your builds. It is built into every Host and cannot be removed.

## How do I set it up quickly?

It opens in **Simple** mode, a checklist of four things:

- **Thinking**: pick a provider you hold a key or sign-in for, and one click routes both text jobs to a recommended model on it, with an optional backup provider. Recommendations are checked against the provider's live model list when applied, and every route stays visible and editable.
- **Memory**: an embeddings model, so your Host can recall by meaning. Run it free on a machine of your own, or use a provider.
- **Seeing**: a model for making images, offered only where the model catalog says the model can make one. Your Host looks at pictures you share with its own Host model, so pick a Host model that reads images.
- **Hearing**: the provider for live voice.

Below the checklist, **What answers when** says which provider and model handles each job and, for a provider you can sign in to, whether your sign-in or your API key answers. If a model fails, you see the error; your Host never quietly swaps in a different one. The only switch to another provider is a backup provider you set yourself, and the answer is marked when the backup gave it.

## What is in Advanced mode?

- **Routing**: the **Host model**, with a provider, model, optional backup and, where the provider accepts one, reasoning effort. It talks, plans, researches, runs agents, reviews what its builds changed and keeps what it learns in memory as it answers. For a second opinion on a build, ask your Host to have a coding engine review it.
- **Coding Engines**: Claude Code, Codex, Cursor, Grok, Kimi Code and Antigravity CLI each run on their own vendor's key or sign-in, and show as not connected without it; none is ever substituted for another. opencode, which has no vendor of its own, runs on your OpenRouter key. A build that names no engine runs on the one you tick as Default; with several connected and none ticked, it stops and asks.
- **Multi-Agent Coding**: protocol files, plain instructions naming which engines run, in turn or at once, and what to do on pass or fail. Your Host can write one; you can edit, check, export and import them. A protocol cannot carry a credential.
- **Embedding**: switching models never re-embeds or deletes what is stored, but entries embedded by the old model are not found by the new one until embedded again.
- **Balances**: what each provider account holds, read live under your own key. Privatae never bills for model use.

More in [Models and keys](wiki:models-and-keys) and [Coding agents](wiki:coding-agents).
