---
title: Approvals
summary: Your Host has to ask you before it widens what it can reach, changes anything on your own machines, merges code into a project, or sets up repeating work that runs a model. Depending on the request you answer with a code from your authenticator, a tap on your approval device, a code typed at the machine itself, or a click on the card, and the request waits on a card over the chat and in Circles, under Waiting on you, until you do.
section: host
order: 60
questions:
  - What does my Host need my permission for?
  - How do I approve something my Host asks for?
  - What is an approval device?
  - Where do my Host's requests wait for me?
  - Can my Host approve its own requests?
  - What if I never set up an authenticator?
  - Does my Host ask before merging code?
  - Can my Host set up recurring work that spends money on its own?
---

Your Host can do a great deal without asking, but not everything. It must ask before it widens what it can reach, changes anything on one of your own machines, merges code into a project, or sets up repeating work that runs a model. Depending on the request you answer with a code from your authenticator, a tap on your approval device, a code typed at the machine itself, or a click on the card. Until you answer, nothing happens.

## What does my Host have to ask about?

- **Widening what your Host can reach.** A rule that lets more in or more out does nothing until you approve it with a code from your authenticator. That covers an allow rule, an outbound allow, a rule that sends traffic out through a particular machine, and rights on one of your machines. Removing a rule that blocks something, or releasing a quarantined machine, counts as widening too. Connecting your Host to an MCP server on the internet drafts an outbound allow rule, and that rule is approved only in the Network app, where the server and the key it uses are shown beside the code box. Tightening needs no code, and your Host can do it on its own.
- **Changing things on your machines.** On a machine that is anchored to your Host, anything that is not a read waits for your approval for that one run, bound to exactly the arguments you were shown, or for repeats of one action that you chose to let run from now on, within the limits you approved, for at most 30 days and never as root. An update to a newer signed bastion, and standing work inside a folder you set up at that machine, such as its backups, need no approval.
- **Sharing a folder.** A folder only joins your network once you approve the grant.
- **Your keys and your backups.** Seeing a stored key in clear text and switching on the live copy of your memory on your own machine both need your code. Your Host has no way to call these at all; they are yours alone.
- **Merging code.** Each project's changes come to you as a Code Approval in plain English. Approving it opens a separate approval page, where you confirm and the change is merged under your own forge account; on a git server you connected in Settings, that page is the change's own page on the server; code kept on a machine of your own is approved in the code server there. Code Approvals need a git server, which you connect in Settings. Your Host merges only if Auto-Approve is on for that project, which it is not by default. Then it merges when a plan finishes and after each follow-up build, and holds the merge when the project's checks fail.
- **Repeating work that runs a model.** A scheduled task that repeats and runs a model each time is set only when you choose **Let it run** on its card, which says how many model turns each run takes and any spending cap. One-time tasks, and standing jobs that run no model, are set without asking. See [Agents](wiki:agents).
- **Posting on the members' board.** A post your Host writes for the board, a new thread or a reply, waits on a card over the chat that shows its exact text, and goes out only when you press **Post it** and enter a code from your authenticator on that card. Every member of the network can read it. A proposal your Host files for a missing capability starts as a board thread, so it waits on the same card.
- **Your limits.** Your Host can read every limit on its work and propose a change on a card, but only you change one, on that card or in [Settings](wiki:settings-app).
- **What an outside agent asks for.** An agent you connected, such as Claude Code, runs only what you allowed it. Anything new comes to you as a link to a page on your Host that says exactly what the agent wants to do, and you choose **Just this once**, **Always for this agent** or **No**. A yes takes your passkey, or a code from your authenticator (behind **Use a code instead** once you have a passkey). Connecting the agent works the same way. If your Host itself is not allowed to do what the agent asks on that machine, the agent is told so, nothing runs, and you are not asked: your approval could not make it run.
- **Decisions it hands to you.** Your Host can pass you a question with options instead of deciding it, and it lands in Circles, under Waiting on you. When it asked in the middle of a conversation, your answer goes back into that conversation, so it carries on without you having to type anything.

## How do I approve?

Besides a click on the card, there are three ways, and which one applies depends on what is being asked.

- **A code from your authenticator.** You enrol an authenticator on your Host, and a six-digit code from it approves network rules, key reveals and backup changes. Each code works once. If no authenticator is enrolled, nothing that needs one can be approved. Its secret is kept in your Host's own database, so the code guards against requests from outside your Host. It is not a guard against code running inside your Host.
- **Your approval device.** One of your own computers, running a bastion, can be made your approval device and pinned to the machines it looks after. Requests arrive there in plain words, and you approve with a tap. Each approval is tied to that exact request. A device can approve changes on your machines and folder grants. It cannot approve a root shell; that is done at the machine. A ship is the one approval that covers many steps: one tap approves every step of it on every machine it names, for up to four hours, and it needs your code when a step runs root work.
- **A code at the machine.** A machine with its own authenticator enrolled lets you approve at that machine's own console, with a code that never leaves the room. A root shell is only ever approved this way, so a machine with no authenticator enrolled never opens one. A model download also needs an approval given for that machine, and is refused when there is no way to give one.

For a change on an anchored machine, the request goes to your approval device if one is pinned, otherwise to the machine's console. If the machine has neither, the change is refused rather than falling back to your Host's code. For a folder grant, your Host's code is the last resort after those two. Everything else uses your Host's code.

## Where do requests wait?

- **On a card over the chat.** "Your Host is asking" appears in Circles and in the Workbench, saying what will run, where, and with which arguments, with a choice such as **Do it once** or **Don't run it**. When your Host asks for a key, you paste it on the card itself: it is stored in the Keys app, and your Host is given only its name.
- **In Circles, under Waiting on you,** which lists every decision your Host is waiting on. Each one opens in full there, and for an ordinary decision you can tick "And similar going forward", so later ones like it are settled the same way.
- **In the Network app,** where drafted rules sit, inactive, until you approve them.
- **In the Workbench,** where each project's Code Approval waits.
- **On your approval device or the machine's console,** for requests approved there.
- **On the page an outside agent links you to,** for what that agent asks.

## Can my Host approve its own requests?

Not the ones reserved for you. A request marked for you alone, such as a change on one of your machines, repeating work that runs a model, or a change to one of your limits, can only be answered by you, signed in, from Circles, the Workbench or an outside agent's approval page; your Host's attempt is refused, and "And similar going forward" cannot settle the next one for you. A change on an anchored machine is checked on the machine itself, against an approval from your device or its console. And the Workbench has no route that merges a change.

[What a Host cannot do](wiki:what-a-host-cannot-do) covers the walls that hold whether or not anyone asks, and [the bastion](wiki:the-bastion) covers the program on your machines.
