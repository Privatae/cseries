---
title: What is C-Series?
summary: C-Series is an open-source AI agent that runs on your own machine. Each running copy is a Host, with its own memory, database, apps and rules, used from a browser, on model keys you bring, behind limits that are enforced in code rather than asked for in a prompt.
section: start
order: 10
questions:
  - What is C-Series?
  - How is it different from a chatbot?
  - Where does a Host run?
  - Do I need a Privatae account?
  - Do my files have to be uploaded to use it?
  - Which AI models does it use?
  - What can I build with it?
---

C-Series is an open-source AI agent (AGPL-3.0-only) that runs on your own machine. Each running instance is called a Host: one AI with its own memory, its own PostgreSQL database, its own apps and its own rules. You use it from a browser on the same machine. It runs sandboxed in a small Linux virtual machine as two containers, the Host and its database, and a separate program on the machine, the [bastion](wiki:the-bastion), decides what the Host may touch there.

## Where does a Host run?

On your machine, inside a Linux virtual machine. The installer sets the machine up and starts the Host; its dashboard answers only on your own machine, on 127.0.0.1. There is no hosted Host: Privatae does not run your Host, and there is no trial or per-use charge for one. [What a Host is](wiki:what-a-host-is) opens it up.

## Do I need a Privatae account?

No. A Host works without one, and installs apps from the App Store without one. A Host can also join the optional Privatae network with a membership code, which adds the community board; membership is not on sale yet. [Joining the Privatae network](wiki:the-privatae-network) says what that connects and what it does not.

## How is it different from a chatbot?

A chatbot is a conversation. A Host acts. It reads what you asked and picks a path: it answers, runs one known operation with no model involved, hands code to a coding engine, or, for untrusted work such as the open web, sends out a narrow agent that starts with no permissions at all. [How a Host handles a request](wiki:how-a-host-handles-a-request) follows each path, and [agents](wiki:agents) explains why an agent here is a worker the Host sends, not something you chat with.

It also remembers. Every conversation is written down in its own database, and the Host notes what is worth keeping in its own turns. A newer note replaces the one it corrects, and the old one is kept on record rather than rewritten. [How a Host remembers](wiki:memory) covers it.

## What stops a Host doing what I did not approve?

Checks in code, not requests in a prompt. An agent it dispatches holds only what it was granted, and the grant is checked at every call. Anything that widens what the Host may reach waits for a code from your authenticator. [What a Host cannot do](wiki:what-a-host-cannot-do) lists the walls.

## What is the bastion?

A separate program, licensed GPL-3.0-or-later, that runs on the machine itself rather than inside the virtual machine. Once it is paired with the Host, the Host can use what the machine offers and you allow: a folder, a model server running there. Nothing is reachable without a grant. [The bastion](wiki:the-bastion) covers the whole of it.

## Do my files have to be uploaded?

No. A folder on your machine joins by a grant you approve. Your Host gets a map of what is there and reads what it needs, one file at a time, only as far as the grant allows. Nothing is synced, and there is no way to delete through it. [Connected folders](wiki:connected-folders) explains how. The Host's own memory is a different thing: it lives in the Host's database, on your machine.

## Whose models does it use?

Yours. You add a key from a provider such as OpenRouter, Anthropic or OpenAI, or point the Host at a model server on another computer on your network, and choose a provider and model for each role the Host has. Privatae does not sell, proxy or bill model usage, and is not an inference provider. [Models and keys](wiki:models-and-keys) has the details.

## What can I build?

You build by talking to your Host in the [Workbench](wiki:workbench). Coding engines you connect write the code, and each change comes back to you as a Code Approval that is merged only when you approve it, unless you have switched on Auto-Approve for that project. A finished project can be installed as an app on your Host. See [building apps](wiki:building-apps).

New to the words? The [glossary](wiki:glossary) defines each one.
