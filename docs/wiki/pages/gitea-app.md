---
title: The Gitea app
summary: Gitea is where you browse the code on your Privatae git server, decide which of your repositories your Host may read or write, and keep copies of your GitHub repositories. It comes with every Host.
section: apps
order: 10
questions:
  - Where is the code my Host writes kept?
  - How do I give my Host access to one of my repositories?
  - Can I take my Host's access to a repository away?
  - How do I bring my GitHub repositories into Privatae?
  - Does mirroring change anything on GitHub?
  - Where do I approve my Host's code changes?
app: gitea
---

Gitea is your window onto the git server where your code and your Host's code live. It comes with every Host. The app is already signed in to that server and makes the calls itself, so there is no token to paste.

## What are the four panels?

- **Your repos**: browse your own repositories inside the app, with their files, commits and branches.
- **Host repos**: the repositories your Host owns. This is its own workspace, where it drafts and builds before offering changes to you.
- **Permissions**: give your Host **Read** or **Write** on any of your repositories, or take it away, one repository at a time, whenever you like. Granting adds your Host as a collaborator on that repository, and your Host starts indexing its code at once so it can search it; revoking removes your Host and drops that index.
- **Mirrors**: keep copies of your GitHub repositories on your git server.

Every link to a code repository anywhere on your Host opens here, because the git server itself answers only the platform's own machines, not the open internet.

## How do GitHub mirrors work?

Connect GitHub once: the app shows a short code, you enter it on GitHub's own site, and the sign-in is saved sealed in [Keys](wiki:keys-app). Then pick repositories. The git server keeps each copy current by pulling on a regular schedule, and **Sync now** pulls straight away. Mirrors only pull: nothing is ever written to GitHub, and no AI model is involved at any point. Mirrored copies are private, even when the GitHub repository is public.

## Where do I approve my Host's changes?

Not here. Your Host's proposed changes come to you as Code Approvals, which you find in the [Workbench](wiki:workbench-app). [Code repositories](wiki:code-repositories) explains how your Host's code gets from its workspace to yours.
