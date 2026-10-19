---
title: Code repositories and approvals
summary: The code your Host writes lives in Code Repositories on a git server Privatae runs. Your Host has its own git account there and so do you. Every change the Host wants to make arrives as a Code Approval in plain English, and approving it merges it as you.
section: building
order: 30
questions:
  - Where does the code my Host writes live?
  - What is a Code Approval?
  - How do I approve a change my Host made?
  - Can I use git on my own computer with my Host's code?
  - Can my Host work on a repository I already have?
  - Can I mirror my GitHub repositories?
---

The code your Host writes lives in Code Repositories on a git server that Privatae runs. Your Host has its own git account there, and so do you. You never have to use git yourself: each change the Host wants to make arrives as a Code Approval written in plain English, and approving it merges the change as you.

## Where does my code live?

On the platform's git server, in repositories that belong either to your Host's own account or to yours. Your repositories on it are not reachable from the open internet, so you reach them through your Host: the [Gitea app](wiki:gitea-app) on your Host lets you browse your repositories and your Host's, and every link to a Code Repository on your Host opens there.

Because we run that server, the code on it sits on systems we operate, like the rest of your Host.

## What is a Code Approval?

It is the plain-English face of a pull request. Each one says what it changes (one line per save point, in the plan's own words), why (the request you made), what was checked, and who wrote each change: the coding engine and model, the Host itself, or a person working in the editor. The file details are folded underneath for anyone who wants them. The format is written by the platform from records, not by a model.

A Workbench project keeps one Code Approval open and adds each landed phase to it as a commit. See [the Workbench](wiki:workbench).

## How do I approve a change?

Open the Code Approval in the Workbench and choose Approve, Ask for changes, or Not now. Approve opens a small window on privatae.ai that shows you the change as your own git account sees it; confirming there merges it as you, pinned to the exact version you were shown. Approval happens in that window, outside your Host's pages, so your Host cannot press Approve for you. Once the merge happens, a line in the project's chat records who merged it and when, read from the git server's own record.

Ask for changes sends your words to the Host, and the Code Approval shows that changes were asked for until the Host saves a new version.

While a project has a Code Approval waiting for you, its tab in the Workbench is lit and says how many are waiting.

The Host merges on its own only when you turn on Auto-Approve for that project, and only where it is allowed to merge.

## Can my Host work on a repository I already have?

Yes. When you start a project you can pick any repository your Host can reach instead of a new one. You grant your Host read or write access to one of your own repositories in the Gitea app, and you can take it back there at any time. Your Host indexes every repository it can read so it can search the code, by meaning once embeddings are set up, and drops the index when its access goes. A repository too large for the room left on your Host's disk is skipped. A repository your Host can only read, or one another project is already using, is offered as a fork: a new private repository under the Host's account holding a copy of its main branch.

When you archive a project, you are asked, unticked, whether to delete its Code Repository too. That is only offered when the Host owns the repository and no other project uses it.

## Can I use git on my own computer?

Yes, through your bastion, the program that joins your own machine to your network (see [the bastion](wiki:the-bastion)). It gives your local git a way to reach the git server through your Host, and a sign-in helper that fetches your git token from your Host each time git asks and stores nothing itself. The setup lines it prints reset git's other credential helpers for that server first, and with a recent git the token is handed over in a form git's own store and cache helpers do not save.

## Can I bring in code from GitHub?

Yes. In the Gitea app you connect GitHub once and pick repositories to mirror into your own account; the git server then keeps them in step with GitHub on its own. Mirrors only pull, so nothing is ever written back to GitHub. Your Host can also import a public repository into a new project.
