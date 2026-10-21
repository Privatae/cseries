---
title: Glossary
summary: Short, plain definitions of the words Privatae uses, such as Host, machine, bastion, the Privatae network, grant, approval, dispatched agent and harness, each with a link to the page that explains it in full.
section: start
order: 90
questions:
  - What does Host mean in Privatae?
  - What is a bastion?
  - What is the difference between a dispatched agent and a coding agent?
  - What is a grant?
  - What is the Privatae network?
  - What is the Workbench?
---

The words C-Series uses, each in a sentence or two, with a link to the page that explains it properly.

## Host

One AI that runs on your own machine, in a Linux virtual machine, as two containers: the Host and its PostgreSQL database. It is where your AI lives, remembers you and does its work. See [what a Host is](wiki:what-a-host-is).

## Machine

A computer. In this release it is the one your Host runs on. C-Series says machine, never "compute". See [your machines](wiki:your-machines).

## Bastion

The small program that runs on your machine, outside the Host's virtual machine, and decides what the Host may touch there. It connects to your Host, and the Host can do only what a grant you approved allows. See [the bastion](wiki:the-bastion).

## The Privatae network

An optional membership. A Host works without it. With a one-time code from your account, a Host joins the network over one outbound connection. See [joining the Privatae network](wiki:the-privatae-network).

## Grant

A permission for something specific. There are two kinds. Your Host grants a dispatched agent a set of capabilities for one task, and the agent can do nothing outside them. And you approve signed grants that let your Host use a folder or a machine within set limits; a folder grant can never include deleting anything. See [agents](wiki:agents) and [connected folders](wiki:connected-folders).

## Approval

Your yes to something your Host may not do on its own, such as widening what it may reach, changing something on your machine, merging code, or setting up repeating work that runs a model. Depending on the request you give it with a code from your authenticator, a tap on your approval device (one of your own computers, set up to approve requests), a code typed at the machine itself, or a click on its card. See [approvals](wiki:approvals).

## Dispatched agent

A throwaway worker your Host sends out for one piece of untrusted work outside your Host, such as reading the open web or someone else's code. It starts with no permissions, gets only what the task needs, cannot run code, and is gone when the task is done. You watch it in the Agents app, but you never talk to it. See [agents](wiki:agents).

## Coding agent, or harness

A coding tool such as Claude Code, Codex, Cursor, Grok, Kimi Code, Antigravity or opencode, running on your own key or sign-in. The harness is the container it runs in, beside your Host rather than inside it, which is why code it writes never runs in your Host. See [coding agents](wiki:coding-agents).

## Circles

The app where you talk with your Host day to day, and with everyone you know, as a deck of cards: each person's chat on the front of their card and their Host on the back. It holds the decisions your Host is waiting on you to answer, your support drafts, and who wants to connect. See [the Circles app](wiki:circles-app).

## Calendar

The app with your own events and the work your Host has scheduled, on one calendar: the month, what is coming up, and every run of each task, which you can cancel. See [the Calendar app](wiki:calendar-app).

## Workbench

The app where you build. Each project is a conversation with your Host plus the work itself. When a project's code is kept on a git forge, its changes come to you as a Code Approval, in plain English, for you to approve. See [the Workbench](wiki:workbench).

## App

A program on your Host's desktop, opened from the dock. Some are built in, others you install from the App Store, which works without a network membership, and you can build your own. See [apps](wiki:apps-overview).

## Site

A website a project can be published as. Sites are served by a public web service that is not part of the Host and that a Host cannot reach in this release. See [publishing sites](wiki:publishing-sites).
