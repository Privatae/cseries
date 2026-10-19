---
title: Glossary
summary: Short, plain definitions of the words Privatae uses, such as Host, machine, bastion, AI DMZ, grant, approval, dispatched agent and harness, each with a link to the page that explains it in full.
section: start
order: 90
questions:
  - What does Host mean in Privatae?
  - What is a bastion?
  - What is the difference between a dispatched agent and a coding agent?
  - What is a grant?
  - What is an AI DMZ?
  - What is the Workbench?
---

The words Privatae uses, each in a sentence or two, with a link to the page that explains it properly.

## Host

Your own machine in the cloud, where your AI lives, remembers you and does its work. It is one pod with its own database, disks, certificate and address on your private network. Host is the word for the product itself. See [what a Host is](wiki:what-a-host-is).

## Machine

A computer. It might be your laptop, a box at home, a server you run, or the one your Host runs on. Privatae says machine, never "compute". See [your machines](wiki:your-machines).

## Bastion

The small program you run on a machine of yours to join it to your private network. It dials out to your Host and holds a tunnel open that reaches your Host and nothing else, so nothing has to dial in. See [the bastion](wiki:the-bastion).

## AI DMZ

What Privatae is. A separate zone for AI work, with no open port, a private door for each of your machines, and limits that are enforced in code and in the network rather than requested in a prompt. See [what Privatae is](wiki:what-is-privatae).

## The network

Your private network: your Host and every machine you have paired with it, each joined by its own tunnel. Your Host can use what a machine offers, such as a folder, a model or its internet connection, only as far as you allow. See [your private network](wiki:your-private-network).

## Grant

A permission for something specific. There are two kinds. Your Host grants a dispatched agent a set of capabilities for one task, and the agent can do nothing outside them. And you approve signed grants that let your Host use a folder or a machine within set limits; a folder grant can never include deleting anything. See [agents](wiki:agents) and [connected folders](wiki:connected-folders).

## Approval

Your yes to something your Host may not do on its own, such as widening your network, changing something on one of your machines, or merging code. You give it with a code from your authenticator, a tap on your approval device (one of your own computers, set up to approve requests), or a code typed at the machine itself. See [approvals](wiki:approvals).

## Dispatched agent

A throwaway worker your Host sends out for one piece of untrusted work outside the platform, such as reading the open web or someone else's code. It starts with no permissions, gets only what the task needs, cannot run code, and is gone when the task is done. You watch it in the Agents app, but you never talk to it. See [agents](wiki:agents).

## Coding agent, or harness

A coding tool such as Claude Code, Codex, Cursor, Grok, Kimi Code, Antigravity or opencode, running on your own key or sign-in. The harness is the disposable sandbox it runs in, beside your Host rather than inside it, which is why code it writes never runs in your Host. See [coding agents](wiki:coding-agents).

## Circles

The app where you talk with your Host day to day, and with everyone you know, as a deck of cards: each person's chat on the front of their card and their Host on the back. It holds the decisions your Host is waiting on you to answer, your support tickets, and who wants to connect. See [the Circles app](wiki:circles-app).

## Calendar

The app with your own events and the work your Host has scheduled, on one calendar: the month, what is coming up, and every run of each task, which you can cancel. See [the Calendar app](wiki:calendar-app).

## Workbench

The app where you build. Each project is a conversation with your Host plus the work itself, and every change the coding agents make comes back to you as a Code Approval, in plain English, for you to approve. See [the Workbench](wiki:workbench).

## App

A program on your Host's desktop, opened from the dock. Some are built in, others you install from the App Store, and you can build and publish your own. See [apps](wiki:apps-overview).

## Site

A website you publish from one of your projects to the public web. A new site is public from the moment it goes live, though search engines are asked to leave it out until you list it. You can put it on your own domain and choose who may get in. See [publishing sites](wiki:publishing-sites).
