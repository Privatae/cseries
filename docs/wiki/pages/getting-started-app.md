---
title: The Getting Started app
summary: Getting Started is the short first-run tour that gets a new Host working, one step and one app at a time, checking each step live. It comes with every Host and opens by itself until you finish or leave it.
section: apps
order: 10
questions:
  - Why did a tour open when I first signed in?
  - What do I need to set up on a new Host?
  - Can I skip the Getting Started tour?
  - How do I get the setup tour back?
  - Do I need my own machine to get started?
app: getting-started
---

Getting Started is a short tour that gets a new Host working. It comes with every Host, opens by itself on a new one until you finish or leave it, and stays in the dock if you want it again.

## What are the steps?

Seven slides, one action each:

1. **Welcome.**
2. **Keys**: add one API key from a model provider in the [Keys app](wiki:keys-app). This step asks for a key you paste yourself.
3. **Orchestrator**: apply the recommended setup in the [Orchestrator's](wiki:orchestrator-app) Simple mode, so every job has a model.
4. **Network**, optional: pair a [bastion](wiki:the-bastion) with your Host, and turn on "Serve my embeddings here" so the model that lets your Host recall by meaning runs on your own hardware. Skip it if you would rather use a provider for that.
5. **Circles**: send your Host one message and get an answer in [Circles](wiki:circles-app).
6. **Workbench**: start a project in the [Workbench](wiki:workbench-app).
7. **Done.**

## How does it know I did a step?

Each slide's button opens the real app. When you close that app, or come back to the tour, the slide checks your Host live: a key is stored, a model is routed, a machine serving embeddings is online, your Host has answered, a project exists. A pass moves you on. If not, the slide says what is missing and offers the same button again. A step you had already done passes by itself, so a reopened tour lands on the first thing left.

The checks only read. The tour creates nothing itself.

## Can I skip it?

Yes. Every app step has a Skip button, and skipping is a real answer. You can also leave the tour at any point, and once you have finished or left it, it stops opening by itself.
