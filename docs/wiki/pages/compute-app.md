---
title: The Compute app
summary: Compute shows what your Host's work has used and what it is estimated to have cost at your model providers, by project, coding engine and run. Estimates and usage are kept apart and labelled. It comes with every Host and only reads.
section: apps
order: 10
questions:
  - What does the Compute app show?
  - How do I see what a project cost me?
  - Why does Compute call some amounts estimates?
  - Does Privatae charge me for AI model usage?
  - Which coding engine gives me the best value for money?
app: compute
---

Compute is your Host's cost-and-usage view. It answers what a project has cost, which coding engine earns its cost, and where your model tokens went. It comes with every Host, and it only reads: nothing is set here. Which model answers which job is chosen in the [Orchestrator](wiki:orchestrator-app).

## Does Privatae charge for model usage?

No. Model calls run on your own keys, and your providers bill you directly. Privatae does not sell, proxy or bill model usage. A Host with no platform connection has no one to charge: a coding run's compute charge is skipped when there is no platform connection, and Compute shows no charged amount for it.

## Why are some amounts labelled as estimates?

Your providers bill you under your own key, and the Host never sees that bill, so Compute works the figure out from token counts and the provider's published rates, and says so. When a coding engine reported a cost for only some of its runs, the figure says how many of them it covers. Usage, such as seconds, tokens or bytes, is counted separately from estimates and is never added to them.

## What are the views?

- **Projects**: what each project has used, its tokens, and how much of its context was served from a provider's cache.
- **Billing**: the meters Compute knows about, one row each, with how each is measured. Billing is not part of this release, so nothing on this tab is charged. A meter that is not measured on your Host says so instead of showing a guessed number.
- **Harnesses**: each coding engine, and each engine-and-model pair, with runs that succeeded and failed, median and 95th-percentile run times, and cost per accepted run. A run counts as accepted when you shipped it, never because a model judged it good.
- **Runs**: one project, one engine, one run, down to the tail of its transcript.
- **Inference**: estimated model spend by background job and by model, including tokens read from and written to a provider's cache.

You choose the window: 7 days, 30 days, 90 days or everything.
