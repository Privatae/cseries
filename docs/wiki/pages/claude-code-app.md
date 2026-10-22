---
title: Claude Code
summary: The Claude Code app shows whether your Claude subscription is connected for Claude Code builds, and lets you disconnect it. The sign-in is Anthropic's own; Privatae hosts no login page for it.
section: apps
order: 10
app: claude-code
questions:
  - How do I connect my Claude subscription to my Host?
  - Can I use my Claude Pro or Max plan for coding with my Host?
  - Where do Claude Code builds run?
  - Does Privatae bill me for Claude Code tokens?
  - How do I disconnect Claude Code from my Host?
---

Claude Code is Anthropic's coding agent and one of the [coding engines](wiki:coding-agents) your Host can build with. This app is its connection panel. It shows whether your Claude subscription is connected, since when, and when the current sign-in expires. Nothing runs inside the app itself.

## How do I connect my Claude subscription?

Through Anthropic's own sign-in; C-Series has no login page of its own for Claude. Ask your Host to connect Claude Code and it starts Anthropic's own login inside the Claude Code container. You sign in on Anthropic's page and give your Host the code that page shows you, and your Host passes it straight to the login. The sign-in is then kept with your Host and handed to the Claude Code container whenever a build runs.

## Whose plan pays for it?

Yours. Claude Code builds run on your own Claude subscription, Pro or Max. The engine is set up to use that sign-in and never an Anthropic API key, so a build cannot quietly switch to pay-per-use billing. Privatae does not bill you for the tokens.

## Where do the builds run?

In their own container beside your Host, never inside it. On a Host installed with `npx cseries`, the local runtime in the Host's Linux system starts that container, and its working files are removed when it is released. You start a build by asking your Host, usually in the [Workbench](wiki:workbench-app), and you watch it there, on the project's page.

## How do I get the app, and how do I disconnect?

The app is not built into the Host; your Host installs it when you ask. To connect again after the sign-in expires, ask your Host to connect Claude Code, which runs Anthropic's own sign-in. **Disconnect** in the app removes the sign-in your Host keeps for builds, in the Vault section of [Keys](wiki:keys-app), and the copy the app reads, and Claude Code stays disconnected until you connect it again, by asking your Host to sign in or by adding the sign-in in Keys. If your Host could not carry it out, the app says Claude may reconnect on its own and asks you to try Disconnect again. When your Host cannot record that Claude Code is disconnected, it removes nothing, because without that record the sign-in would come back on its own.
