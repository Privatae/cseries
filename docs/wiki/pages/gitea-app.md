---
title: The Gitea app
summary: Gitea is where you browse the code on the git server your Host uses and see the repositories your Host works in. You connect a git server you run in Settings; until you do, there is none. On a server you connected, you give your Host access to your repositories, and mirror GitHub, on the server itself.
section: apps
order: 10
questions:
  - Where is the code my Host writes kept?
  - How do I give my Host access to one of my repositories?
  - Can I take my Host's access to a repository away?
  - How do I bring my GitHub repositories into my forge?
  - Does mirroring change anything on GitHub?
  - Where do I approve my Host's code changes?
  - How do I connect my own git server?
app: gitea
---

Gitea is your window onto the git server where your code and your Host's code live. It comes with every Host, but the git server is a separate Gitea or Forgejo server and not part of the Host. You connect one you run in [Settings](wiki:settings-app), under Git server. Until you do, the app says no git server is connected.

On a git server you connected, your Host signs in only as the account you made for it there, and the app has no sign-in of yours. Your own repositories, giving your Host access to one of them, and GitHub mirrors are then done on the server's own pages, signed in as you. The app refuses to do them itself: Your repos and Permissions say where to go instead and name your Host's account there. Host repos works as below.

## What are the panels?

- **Your repos**: browse your own repositories inside the app, with their files, commits and branches.
- **Host repos**: the repositories your Host owns. This is its own workspace, where it drafts and builds before offering changes to you.
- **Permissions**: on a git server you connected, this panel tells you to open the repository's settings on the server, signed in as yourself, and add your Host's account as a collaborator with read or write access; removing it there takes the access back. On a git server your Host's install names, where you sign in to the app as yourself, the panel itself gives your Host **Read** or **Write** on any of your repositories, or takes it away, one repository at a time. Either way your Host indexes a repository's code as soon as it can read it, and drops that index when the access goes.
- **Mirrors**: keep copies of your GitHub repositories on your forge. This panel appears only when the Privatae network offers mirroring to your Host, which it does not in this release, so today there is no Mirrors panel.

Every link to a code repository anywhere on your Host opens here.

## How do GitHub mirrors work?

On a git server you connected, you make a mirror on the server itself, signed in as yourself (on Gitea, New Migration, then GitHub); the app will not make one there. Where the Mirrors panel is shown, you connect GitHub once: the app shows a short code, you enter it on GitHub's own site, and the sign-in is saved sealed in [Keys](wiki:keys-app). Then pick repositories. The forge keeps each copy current by pulling on a regular schedule, and **Sync now** pulls straight away. Mirrors only pull: nothing is ever written to GitHub, and no AI model is involved at any point. Mirrored copies are private, even when the GitHub repository is public.

## Where do I approve my Host's changes?

Not here. Your Host's proposed changes come to you as Code Approvals, which you find in the [Workbench](wiki:workbench-app). [Code repositories](wiki:code-repositories) explains how your Host's code gets from its workspace to yours.

## How do I connect my own git server?

In [Settings](wiki:settings-app), under Git server. Make an account on the server for your Host and an access token for it, then give Settings the server's address, that username and the token.
