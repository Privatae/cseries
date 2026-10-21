---
title: Building with the Workbench
summary: You build by talking to your Host in the Workbench. The Host plans the work in phases, coding engines build it outside the Host, and every change comes back to you as one plain-English Code Approval that you approve.
section: building
order: 10
questions:
  - How do I build an app by talking to my Host?
  - Does the Host make a plan before it starts coding?
  - Can the Host merge its own code?
  - What is Auto-Approve?
  - What happens if a build phase fails?
  - How do I stop a build that is running?
  - What does the bench in the Workbench show?
---

The Workbench is where you build things by talking to your Host. Each project is a conversation and the work itself: you say what you want, the Host plans it, coding engines build it, and every change comes back to you as a Code Approval that you approve. The Host does not merge your code unless you turn on Auto-Approve for that project.

## What is a project?

A project is one conversation with your Host plus the code it produces. You start one by typing what you want to work on; the Workbench shows any existing projects that match, so you can pick one of those instead. Each project is attached to one Code Repository: a new one made for it, which is the default, or one your Host can already reach. A repository that another project is using, or that your Host can only read, is offered as a copy (a fork) into the new project. See [Code repositories and approvals](wiki:code-repositories).

Each project is a page of the Workbench's book, newest first. Tabs on the book's edge turn to the newest seven and light up when something lands on a page you are not reading. The Index, the tab under them, finds any page, open or archived, by its words and its conversation. Writing a message there and pressing Enter gives it to the page your Host picks, which says why it chose that page, or you can have it start a new page instead. Archived pages sit in chapters you name.

Each page shows a running count of the tokens, and what they cost, of every Host turn on it. While your Host is working on a page, Send becomes Nudge: what you write reaches the turn already running without stopping it, and Stop stays beside it.

## Does the Host make a plan before it builds?

For a phased build, yes. The Host writes a plan cut into phases, each with the brief the builder will be given, and shows it to you, and phases are built only from a plan. A single build, which you or your Host can start at any time, runs one task without a plan.

A phase can be marked as needing your sign-off. That phase cannot start until you press Approve on it in the Workbench, or on its card in Circles, and approving it starts it as soon as what it waits for has landed. The Host's own tools cannot approve it for you. Phases that do not depend on each other can build at the same time; a phase that waits for another says so.

## Where does the code get written?

Not inside your Host. Each build runs on a coding engine you connected, in a container of its own that the local runtime starts beside your Host, inside the same virtual machine, under your own account or key. See [Coding agents](wiki:coding-agents).

## Who merges the code?

You do. Every phase that lands is saved as a commit on one rolling Code Approval for the project, written in plain English: what changed, why, what was checked, and the engine and model each change was built with. When you press Approve, a separate approval page opens, and confirming there merges the change as you, with your own git account. Approval happens there, outside your Host's pages, so your Host cannot press Approve for you. Code Approvals are pull requests on a git forge, which a Host from the standard installer does not have configured; see [Code repositories and approvals](wiki:code-repositories).

## What is Auto-Approve?

Auto-Approve is your standing permission for the Host to approve its own Code Approvals in one project. It is off by default, sits beside the chat box, and is only offered where the Host is allowed to merge in that repository.

With it on, the Host merges when the last phase of the plan lands, or when a follow-up build lands. It holds off, and tells you why, when the build checks found problems, when git reports a conflict, when the repository's own automated checks have not passed that exact version, or when a review of the change blocked it. Every merge it makes is recorded as the Host's and announced in the project's chat. With Auto-Approve on, a finished app is also installed on your Host, started, and checked that it actually runs.

## What happens if a phase fails?

When the plan is advancing on its own and a phase fails, it gets one retry on the same engine with the failure passed back to it. If that also fails, the chain stops and says so. A failure on the infrastructure's side, such as a coding engine that could not be started or a connection that dropped, does not use up that retry; if the infrastructure fails the same phase twice, the chain stops and says the cause was not your project. A run you or your Host killed is never retried. A build your Host started outside the plan goes back to it when it fails, with what the run was doing when it stopped, so the Host can run it again or ask you what only you can decide. By default, after three failed runs in a row in one conversation, further failures are written there but wake your Host no more until you write; that number is one of your limits, which you can change.

## Can I stop a build?

Yes, in two ways. Say stop and nothing further starts; work that is already running finishes and is reported.

To end a run now, press **kill** on its run in the conversation, or tell your Host to kill it. That is for a run that keeps working and gets nowhere, which the automatic stop for idle runs never catches. The run ends on its coding engine, what it wrote so far is kept, and the project's plan stops advancing on its own until it is built again. Any other run on the same coding engine at that moment stops with it, and you are told which.

## What is the bench?

The bench is on the right side of each page. It shows the page's run as it happens, one line per engine with the steps it takes, and a scoreboard when several engines take on the same task. Under it are the project's modules and their versions, the plan with each phase's state, and a numbered log of every coding run with its engine, model, result and time. You can point at a run by its number in chat ("look at run 14") and the Host finds it. Each engine's own steps show in the conversation, on its card.

## What can I do with a finished project?

Install it on your Host as an app ([Building apps](wiki:building-apps)), under Ship this project on the right side of the project's page. Publish as site sits there too, but in this release it cannot complete; [Publishing a website](wiki:publishing-sites) says why.
