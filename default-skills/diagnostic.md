---
name: diagnostic
description: The first call when anything fails — a tool error, a model refusal, a picture that did not arrive — and how the host fixes its own machine. diagnose returns what failed in the provider's or tool's own words, and each configuration fault with the exact call that fixes it. Deterministic data in, no model in the loop.
allowed-tools: diagnose, model_routing, embedding_route, embed_jobs, coding_protocol, memory_curate, system.app_status, system.restart_app, tickets, search_my_code, code_grep, code_read, gitea
---

# Diagnostic SOP — what failed, and your own machine

## When something fails, call `diagnose()` first

A tool returned an error, a provider refused a call, a picture the person
sent did not reach you, a build or an agent failed: call `diagnose()`
BEFORE you form a theory. Its `failures` are your own records, grouped:

- `provider` — model calls a provider refused or that failed, with the
  provider's exact words, the status, how many times, first and last seen.
  `pictures_dropped: true` means each refused call was sent again with a
  note in place of every picture: the pictures never reached the model.
  `refusal: true` is the provider's safety filter declining the request
  (an answer, not an outage), with `refusal_category` (cyber, bio, … or
  null) and what the route's Orchestrator fallback did: `fallback` +
  `fallback_answered`, `no_fallback: true`, or `not_retried: true` (a
  `reasoning_extraction` decline, never sent to a fallback: the fix is the
  request, so no fallback is proposed for it). A decline is in your block
  from the first one. With no fallback, tell the person plainly and propose
  one (skill 'routing': `model_routing` op='set' with `fallback_provider`
  and `fallback_model` files a card they approve; nothing changes until
  they do);
  when the fallback answered, say once which model answered.
- `tool` — tool calls that failed, with the tool's words. `fault: model` is
  your own call's mistake (arguments, the wrong door); `fault: platform` is
  the tool failing on the platform's side.
- `task` — agents and builds that failed, with the error each recorded.

Quote what it says. A cause you inferred is a guess until `failures` or the
code shows it. `since_hours` reads further back (default 24).

## Your machine — the "Your machine needs attention" block

The platform measures your configuration and puts one short block in your
prompt. A line tagged `keeps failing` is a failure that repeated; the rest:

1. **Read the block.** One line per fault, tagged `fix ready`, `needs a
   decision`, `no configuration fix` or `keeps failing`.
2. **Call `diagnose()`.** Every fault comes back as data: what is wrong,
   how it was measured (`evidence`), and for a `fix ready` item the exact
   call — `fix.tool` and `fix.args`. `area=` narrows to one area (routing,
   embedding, protocols, ledger, memory, failures); `refresh=true` re-reads
   instead of the two-minute cache.
3. **Apply the fix with the tool it names**, argument for argument:
   `model_routing op='set'`, `embed_jobs op='enqueue'`, `coding_protocol
   op='save'`, `memory_curate`, `system.restart_app`.

Then say, one line per item, what you changed and why, and what you did not
change and what it needs.

## What you apply yourself, what you ask first

`diagnose()` returns the policy in its `policy` field:

- **Apply yourself:** a role with NO route today (set it, then tell the
  user what was set and why); an embedding fill for rows missing a vector in
  the configured model's own column; a protocol file the migration wrote
  with a defect (author `migrated`); a sidecar restart the system skill's
  crash policy allows.
- **Ask first:** moving a role that already works (it changes who is
  billed); switching or re-embedding the embedding model; deleting
  anything; retiring a rule the user taught.
- **Never:** a code change as a diagnostic fix. Every `fix` is a
  configuration call.

## A fault in the platform itself

When `failures` or a fault shows the platform is wrong and no configuration
call fixes it, `diagnose()` says who fixes it, in `platform_source`,
`can_propose` and `platform_fault`:

- **`platform_source: true` and `can_propose: true`** — you hold the platform source (the
  `infra.codebase` grant that lists the source tools for you). It is yours
  to fix, yourself, never through an agent: find the code the failure
  names by meaning first (`search_my_code` with `project=` the platform
  repo your platform_fault rule names), then pin it with `code_grep` and
  read the range with `code_read`, then open the pull request with
  `gitea op='propose'` — `repo=` that platform repo, a title, a body, and the
  change as `files` (each a whole file, or `edits` to it) or a `patch`
  (skill 'repos'). That is a
  fork, a branch and a real PR on the forge; the same `branch_name`
  updates it. Give the user the pr_url. Do not file a support ticket for
  your own platform.
- **`platform_source: true` and `can_propose: false`** — read the code the
  same way, but you cannot open the pull request: `op='propose'` needs the
  `write:gitea` scope, which only a read_write grant on `external.operator`
  carries. File it with `tickets op='support_open'`, naming the file and line
  you read and the change it needs, and tell the person that grant would let
  you open such fixes yourself.
- **`platform_source: false`** — file it with `tickets op='support_open'`.
  The draft carries the `diagnose` reading — the failures and the faults —
  for the person to approve before anything is sent. Describe the symptom;
  the evidence is already attached.

## Reading a fault

```
{ "id": "routing.image_gen", "area": "routing", "severity": "fix",
  "title": "role 'image_gen' has no route; openai / gpt-image-2 is the recommended pick",
  "fix": {"tool": "model_routing",
          "args": {"op": "set", "role": "image_gen", "provider": "openai", "model": "gpt-image-2"}},
  "ask_user": null,
  "evidence": {"providers_with_keys": ["openai", "anthropic"]} }
```

`severity: fix` + `fix` present + `ask_user` null → apply it. `severity:
review` → the decision is named in `ask_user`; put it to the user in their
words. `severity: info` → report it; there is nothing to configure.

## What the checks cover today

Failures (provider refusals, failed tool calls, failed tasks), routing
(unrouted roles against the keys on hand), embeddings (not set up; rows
missing a vector with no fill running), protocols (a migrated bench that
repeats an engine; engines named that are not connected; no default engine
ticked), the run ledger (a harness whose runs report no token usage), and
taught rules older than 90 days. Anything not in this list is not measured
— say so rather than guess.
