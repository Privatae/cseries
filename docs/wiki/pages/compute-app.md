---
title: The Compute app
summary: Compute shows what your Host's work has cost and used, by project, billable, coding engine and run, with real charges, estimates and usage kept apart. It comes with every Host and only reads.
section: apps
order: 10
questions:
  - What does the Compute app show?
  - How do I see what a project cost me?
  - Why does Compute call some amounts estimates?
  - Does Privatae charge me for AI model usage?
  - Which coding engine gives me the best value for money?
  - What does "unbilled" mean on a run?
app: compute
---

Compute is your Host's cost-and-usage view. It answers what a project has cost, how each thing Privatae charges for is measured, which coding engine earns its cost, and where your model tokens went. It comes with every Host, and it only reads: nothing is set here. Which model answers which job is chosen in the [Orchestrator](wiki:orchestrator-app).

## Why are some amounts labelled as estimates?

Compute keeps three kinds of number apart and labels every figure with its kind:

- **Billed** is what the platform actually charged you.
- **Estimate** is what your model providers are likely to charge. They bill you directly, under your own key, and Privatae never sees that bill, so Compute works the figure out from token counts and says so. When a coding engine reported a cost for only some of its runs, the figure says how many of them it covers. None of it is billed by Privatae.
- **Usage** is resources counted in seconds, tokens or bytes. Some of it is charged and some is counted only so you can see it; the Billables view says which, row by row.

Provider estimates are always shown on their own, never folded into what you were billed.

## What are the views?

- **Projects**: what each project has cost, its tokens, and how much of its context was served from a provider's cache.
- **Billables**: everything Privatae charges for, how it is measured, and whether it is charged. A row that is not measured yet says "not metered yet" instead of showing a guessed number.
- **Harnesses**: each coding engine, and each engine-and-model pair, with runs that succeeded and failed, median and 95th-percentile run times, and cost per accepted run. A run counts as accepted when you shipped it, never because a model judged it good.
- **Runs**: one project, one engine, one run, down to the tail of its transcript.
- **Inference**: estimated model spend by background job and by model, including tokens read from and written to a provider's cache.

You choose the window: 7 days, 30 days, 90 days or everything.

## What does "unbilled" mean?

A run marked unbilled used sandbox time whose charge never reached the billing ledger. Compute shows it so it can be checked. It is a fault marker, not a discount.

Prices are on https://privatae.ai/pricing, and [Plans and billing](wiki:plans-and-billing) explains what you pay for.
