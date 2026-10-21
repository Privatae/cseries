---
title: Code repositories and approvals
summary: The code your Host writes lives in repositories on a git forge, and every change arrives as a Code Approval in plain English that you approve. The forge is a separate Gitea server, and a Host from the standard installer has none configured, so this part is not usable on it yet.
section: building
order: 30
questions:
  - Where does the code my Host writes live?
  - What is a Code Approval?
  - How do I approve a change my Host made?
  - Does a Host from the installer have a git forge?
  - Can I mirror my GitHub repositories?
---

The code your Host writes lives in Code Repositories on a git forge (a Gitea server), and each change the Host wants to make arrives as a Code Approval written in plain English. A Host does not contain a forge. It uses one that has been configured for it, with an address and a credential of its own. A Host installed with `npx cseries` has none, so on it the Gitea app and Code Approvals report that no forge is configured, and this page describes what they do once one is.

## Where does my code live?

On the configured forge, in repositories that belong either to your Host's own account there or to yours. Your Host keeps a copy of every repository it can read and an index of that code in its database so it can search it. A repository too large for the room left on the Host's disk is skipped. Every link to a Code Repository on your Host opens in the [Gitea app](wiki:gitea-app).

## What is a Code Approval?

It is the plain-English face of a pull request. Each one says what it changes (one line per save point, in the plan's own words), why (the request you made), what was checked, and who wrote each change: the coding engine and model, the Host itself, or a person. The file details are folded underneath for anyone who wants them. The format is written from records, not by a model.

A Workbench project keeps one Code Approval open and adds each landed phase to it as a commit. See [the Workbench](wiki:workbench).

## How do I approve a change?

Open the Code Approval in the Workbench and choose Approve, Ask for changes, or Not now. Approve opens a separate approval page, which by default is the Privatae site, that shows you the change as your own forge account sees it; confirming there merges it as you, pinned to the exact version you were shown. Approval happens in that page, outside your Host's own pages, so your Host cannot press Approve for you.

Ask for changes sends your words to the Host, and the Code Approval shows that changes were asked for until the Host saves a new version. The Host merges on its own only when you turn on Auto-Approve for that project, and only where it is allowed to merge.

## Can my Host work on a repository I already have?

Yes, where a forge is configured. When you start a project you can pick any repository your Host can reach instead of a new one. You grant your Host read or write access to one of your own repositories in the Gitea app, and you can take it back there at any time. Your Host indexes every repository it can read, and drops the index when its access goes. A repository your Host can only read, or one another project is already using, is offered as a fork: a new private repository under the Host's account holding a copy of its main branch.

## Can I bring in code from GitHub?

Yes, where a forge is configured. In the Gitea app you connect GitHub once and pick repositories to mirror into your own account; the forge then keeps them in step with GitHub on its own. Mirrors only pull, so nothing is ever written back to GitHub.
