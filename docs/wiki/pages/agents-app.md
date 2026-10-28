---
title: Agents
summary: The Agents app shows everything your Host has running for you, from agents it dispatched and builds on your coding engines to scheduled work, and lets you stop any scheduled job with one click. It comes with every Host.
section: apps
order: 10
app: agents
questions:
  - How do I see what my Host is doing in the background?
  - What agents is my Host running right now?
  - Which of my recurring tasks actually use an AI model?
  - What does my scheduled work cost?
  - How do I stop a scheduled job my Host set up?
  - Why does the Agents app say my Host has no model?
---

Agents is where you watch the work your Host is doing for you. It comes with every Host. You do not start anything here: your Host starts the work, and this window shows it.

## What does it show?

Four kinds of work, on the tabs **All**, **Dispatched**, **On the pool** and **Scheduled**:

- **Dispatched**: an [agent](wiki:agents) your Host sent off to do one task. A model runs.
- **On the pool**: a build or a comparison running on your [coding engines](wiki:coding-agents). A model runs there too.
- **Scheduled**, which holds two kinds. A scheduled turn is work your Host does on a clock by thinking it through each time, so a model runs on every run; one that repeats is set only after you approve it (see [Approvals](wiki:approvals)). A standing job is one fixed operation run on a schedule, and no model runs at all.

A strip at the top counts how many scheduled runs needed a model and how many did not.

## What can I see about each agent?

Its title, whether it is running, queued, done or failed, how long it has run, how many tools it has called and which model it ran on. Pick one to see where that model came from (named by your Host for the task, your preference or your Host's for that kind of work, or your Host's own model) and to follow it live: the tools it was allowed, then its transcript as it works, with its reasoning, what it wrote, each tool it called and what came back, and any error.

## What does scheduled work cost?

Each scheduled turn shows what it spent in the last 24 hours, added up from the runs that carried a price, and how many runs in that time had no price. A run with no price is counted, never shown as $0. Each run's report in its thread also says what that run cost, when it had a price.

A standing job shows "no model" instead of a cost: no model ran, so there is no model bill, and a zero would look the same as a price nobody knows.

## How do I stop scheduled work?

Every scheduled item has a **Stop** button. One click and it will not run again. There is no confirmation step, and you do not need to ask your Host.

## Why does it say "Your host has no model"?

Your Host has not been given a model yet, so the agents it tries to send off cannot run. Choose one in the [Orchestrator](wiki:orchestrator-app), on a key you hold or a model running on one of your own machines. [Models and keys](wiki:models-and-keys) explains the choice.
