---
title: Workbench
summary: The Workbench is where you build with your Host. Each project is a conversation with your Host, its builds run live beside the chat, and, once you connect a git server, every change comes to you as a Code Approval. It comes with every Host.
section: apps
order: 10
questions:
  - What is the Workbench app?
  - How do I start a project with my Host?
  - Where can I watch my Host's builds running?
  - How do I approve the changes my Host made?
  - What is Auto-Approve?
  - How do I turn a finished project into an app?
app: workbench
---

The Workbench is the window where you build things with your Host. Each project is a conversation with your Host plus the work itself, and everything it builds runs where you can watch it. It comes with every Host. [The Workbench](wiki:workbench) explains how a build is planned and carried out.

## How do I start a project?

Write what you want in the Index, the tab under the book's tabs, and press Enter: your Host gives your message to the page it belongs on, or, when none clearly fits, to the page you have open. If it picks the wrong page, choose “starts a new page” under the box and Enter makes a page of its own for your words instead. A book with no pages yet offers Start instead. Once a git server is connected, a new project gets a new Code Repository by default; in Start you can also pick one your Host can already reach. A repository that already belongs to another project, or that your Host can only read, is offered as a fork instead. [Code repositories](wiki:code-repositories) covers them in full.

In the chat you talk the work through with your Host. You can attach files to a message, and they become documents your Host can read. While your Host is working, Send becomes Nudge: your words reach it before its next step, without stopping it. Holding Send, or Enter, asks your Host to think harder about that one message, where its model offers that. The plan appears as phases, and a phase that needs your say shows "awaiting your approval" with an Approve button.

## Where do I watch it build?

On the project's page, and on the bench at its right. Each coding run appears in the conversation as a card while it works: every command, file read and file written is one row with how long it took and whether it worked, the engine's own messages read as text, and the bottom line says what it is doing now. When the run finishes, the page it built loads in the card, so you can scroll the conversation and see each version as it looks, with the files it changed and how many lines below. Click it to open that version full size, with its changes beside it. This page, on the right, also opens the page's latest finished build, so you do not have to scroll back to its run.

Every run also shows on the bench and stays there after it finishes, with its engine, its model and how long it took. Runs are numbered, so you can refer to one by its number when you talk to your Host. [Coding agents](wiki:coding-agents) covers the engines that do the building.

## How do I put a project away?

Archive it from This page, on the right side of its page. It leaves the book's tabs for its chapter in the Index, and writing on it again brings it back. Archiving keeps everything; your Host's Code Repository for it is deleted only if you ask, and only after you confirm by typing its name.

## How do I approve a change?

Each change arrives as a Code Approval: a plain-English description of what it changes, with the full change folded underneath. Approve opens a separate approval page that puts the change in place as you, not as your Host. On a git server you connected in [Settings](wiki:settings-app), that page is the change's own page on the server, where you approve it signed in as yourself, and the card says which server it opened. Code Approvals need a git server; until you connect one, the Workbench says so. Ask for changes sends your words back to your Host as your next message.

Auto-Approve, beside the chat input, lets your Host approve its own changes in that project. It is offered only where your Host is able to merge, it is off unless you turn it on, and it holds back a change that has build-contract problems or a conflict. Each change it approves is noted in the chat.

## What can a finished project become?

An app on your own Host, with Install as my app; [Building apps](wiki:building-apps) goes further. Publish as site is offered too, but in this release it cannot complete; [Publishing a website](wiki:publishing-sites) says why.
