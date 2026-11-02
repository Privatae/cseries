---
title: Code repositories and approvals
summary: The code your Host writes lives in repositories on a git server, and every change arrives as a Code Approval in plain English that you approve. The git server is a separate Gitea or Forgejo server that you run and connect in Settings.
section: building
order: 30
questions:
  - Where does the code my Host writes live?
  - What is a Code Approval?
  - How do I approve a change my Host made?
  - How does my Host get a git server?
  - Can I mirror my GitHub repositories?
---

The code your Host writes lives in Code Repositories on a git server (a Gitea or Forgejo server), and each change the Host wants to make arrives as a Code Approval written in plain English. A Host does not contain a git server. You connect one you already run in [Settings](wiki:settings-app), under Git server, with an account on it that you made for your Host and that account's token. Until you do, the Gitea app and Code Approvals say no git server is connected, and this page describes what they do once one is.

## Where does my code live?

On the connected git server, in repositories that belong either to your Host's own account there or to yours. Each project's new repository is made under your Host's account, and if you gave Settings your own username there, your Host gives you write access to it. Your Host keeps a copy of every repository it can read and an index of that code in its database so it can search it. A repository too large for the room left on the Host's disk is skipped. Every link to a Code Repository on your Host opens in the [Gitea app](wiki:gitea-app).

## What is a Code Approval?

It is the plain-English face of a pull request. Each one says what it changes (one line per save point, in the plan's own words), why (the request you made), what was checked, and who wrote each change: the coding engine and model, the Host itself, or a person. The file details are folded underneath for anyone who wants them. The format is written from records, not by a model.

A Workbench project keeps one Code Approval open and adds each landed phase to it as a commit. See [the Workbench](wiki:workbench).

## How do I approve a change?

Open the Code Approval in the Workbench and choose Approve, Ask for changes, or Not now. On the git server you connected, Approve opens the change's own page on that server, and you merge it there signed in as yourself; your Host sees the merge the next time it reads the server, and the chat says whether you or your Host approved it. Approval happens in that page, outside your Host's own pages, so your Host cannot press Approve for you.

Ask for changes sends your words to the Host, and the Code Approval shows that changes were asked for until the Host saves a new version. The Host merges on its own only when you turn on Auto-Approve for that project, and only where it is allowed to merge.

## Can my Host work on a repository I already have?

Yes, once a git server is connected. When you start a project you can pick any repository your Host can reach instead of a new one. On a git server you connected in Settings, you give your Host access on the server itself: open the repository's settings there, signed in as yourself, and add your Host's account as a collaborator with read or write access; removing it there takes the access back. The Gitea app's Permissions panel does not grant on a connected server; it tells you to do this and names your Host's account. Only on a git server your Host's install names, where you sign in to the app as yourself, can you grant and take back access from the Permissions panel. Your Host indexes every repository it can read, and drops the index when its access goes. A repository your Host can only read, or one another project is already using, is offered as a fork: a new private repository under the Host's account holding a copy of its main branch.

## Can I bring in code from GitHub?

Yes, on the git server itself. On a git server you connected in Settings, your Host does not make mirrors: you make one on the server's own pages, signed in as yourself (on Gitea, New Migration, then GitHub), and the server keeps it in step with GitHub. The Gitea app has a Mirrors panel only when the Privatae network offers mirroring to your Host, which it does not in this release. Either way a mirror only pulls, so nothing is ever written back to GitHub.

## How does my Host get a git server?

You connect one you already run, a Gitea or Forgejo server reached over https, in [Settings](wiki:settings-app) under Git server. If your Host's install already names a git server, it uses that one until you connect another.
