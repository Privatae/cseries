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
  - How do I name my Host?
  - Do I need a Privatae account to get started?
app: getting-started
---

Getting Started is a short tour that gets a new Host working. It comes with every Host, opens by itself on a new one until you finish or leave it, and stays in the dock if you want it again.

## What are the steps?

Nine slides, one action each:

1. **Welcome.**
2. **Name your Host**: a new Host is called "C-Series". The button opens [Settings](wiki:settings-app) at About, where you type the name you want and save it. Skip to keep "C-Series"; you can change it in Settings any time.
3. **Use the model on this machine**: shown only when your Host has no model chosen yet and a chat model is served on the computer your Host runs on, on a machine it is in charge of, or by a model server you added in the Orchestrator. The button, which names the model, makes your Host think with it: no account and no key. A Host with no key already uses that model until you choose another.
4. **Keys**: add one API key from a model provider in the [Keys app](wiki:keys-app). This step asks for a key you paste yourself. After the step before it, a key is optional.
5. **Orchestrator**: apply the recommended setup in the [Orchestrator's](wiki:orchestrator-app) Simple mode, so every job has a model.
6. **Join the Privatae network**, optional: apps install from the [App Store](wiki:app-store-app) without any account. A membership code from your Privatae account page, entered in Settings under Network, adds the [Privatae network](wiki:the-privatae-network). The button opens Settings at that tab.
7. **Circles**: send your Host one message and get an answer in [Circles](wiki:circles-app).
8. **Workbench**: start a project in the [Workbench](wiki:workbench-app).
9. **Done.**

## How does it know I did a step?

Every slide's button opens the real app. When you close that app, or come back to the tour, the slide checks your Host live: it has a name other than "C-Series", a key is stored, a model is routed, your Host has joined the network, your Host has answered, a project exists. A pass moves you on. If not, the slide says what is missing and offers the same button again. A step you had already done passes by itself, so a reopened tour lands on the first thing left.

The checks only read. The tour creates nothing itself, and the name is saved in Settings, not by the tour. The one step that acts on its own is the model on your machine, and only when you press its button.

## Can I skip it?

Yes. Every step that opens an app has a Skip button, the name step included, and skipping is a real answer. You can also leave the tour at any point, and once you have finished or left it, it stops opening by itself.
