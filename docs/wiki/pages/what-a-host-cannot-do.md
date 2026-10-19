---
title: What a Host cannot do
summary: The limits on a Host are checks that run in code, not requests in a prompt. An agent starts with nothing, every call is checked per operation, a dispatch that cannot succeed is refused before it starts, anything that widens your network waits for your code, and nothing the Host dispatches can run code.
section: host
order: 70
questions:
  - What is my Host not allowed to do?
  - Can an AI agent on Privatae go rogue and delete my files?
  - Can my Host give itself more permissions?
  - Can my Host change my firewall rules without asking me?
  - Can my Host run code on its own?
  - Are these limits just instructions in a prompt?
---

"Cannot" here means a check that runs in code whether or not anyone remembered to ask for it. It is not a line in a prompt asking a model to behave, which a model can misread, forget, or be talked out of by something it reads mid-task. These are the walls in this release.

## Why does an agent start with nothing?

An agent your Host dispatches holds no capability at all when it starts. It cannot read your memory, write anything or reach the network until the Host grants it something, by name, for that one task. There are seven names to choose from. Two more capabilities exist, administering the Host and running a shell, and the grant itself refuses both, so no agent can ever hold them. A misspelt name is refused with the list of valid ones, never matched to something close.

The capabilities are never worked out from the tools the agent was given. The agent acts under the overlap of the Host's own permissions and the grant, so it can never exceed the Host that made it or the task it was made for, and it cannot raise its own grant.

## Why is the check made per operation?

Because one tool often does two things. The tool that searches memory can also add to it, so the gate looks at the operation being called rather than the tool as a whole. An agent granted read access can search and is refused when it tries to add. An operation nobody has classified yet falls back to the tool's full requirement, so a gap in that table can only narrow what an agent may do, never widen it.

A tool the agent was never given is refused at the moment it is called, with a message telling it that widening the grant is the Host's decision, not its own. Each refused call shows up in the Agents app as a red entry naming the tool, so a wall is never silent.

## What happens when the Host gets a grant wrong?

The dispatch is refused at the door, before any task exists. If the Host gives an agent a tool without the capability that tool needs, the refusal names each tool and what it is missing, and spells out the grant that would work. No worker starts, so you never hear about a job that failed, because none began. A dispatch whose every tool is a single known operation is refused too, and the Host has to make those calls itself.

## What can the Host not do even with every capability?

Widen your network without your code. A rule that lets more in or more out commits only with a code from your authenticator, and so do removing a rule that blocks something and releasing a quarantined machine. Tightening needs no code. With no authenticator enrolled, no widening can be committed at all. Each code works once, and repeated wrong codes make you wait before trying again.

Once you approve a rule, its terms are signed with your Host's key, and the gate enforces only signed terms. On your private network, a rule changed after approval puts traffic into quarantine. On traffic that passes the gate, which is open until you narrow it, a changed or unsigned rule is ignored, which can only ever widen reach and never cut your Host off. The authenticator's secret is kept in your Host's own database, so this guards against requests from outside your Host, not against code running inside it or against us.

## Why can nothing it dispatches run code?

Because that boundary keeps a mistake from becoming damage: an agent can misconfigure something or send the wrong message, but it cannot run code. Any tool that runs code is stripped from an agent before it starts, refused again if asked for anyway, and refused by the known-operation path too. The list is drawn up from what each tool actually runs, with name patterns behind it that catch new tools shaped like code runners.

Code runs only through the Host's own build tools, in a separate coding sandbox and never inside the Host's own container. [Coding agents](wiki:coding-agents) describes those sandboxes. The one other place your Host can write something that runs is its own database, where it keeps what it builds for you: functions in the database's own languages. They can read and change only the part of the database your Host owns, they cannot reach files or the network, they cannot read your keys, and they cannot change your approvals or the record of what your Host did. Besides those, your Host can set up a service in a container on one of your own machines, only under a grant you approved for that machine that names the images it may run. It can also run commands as root on a machine it anchors, one at a time, only after you allowed that at the machine itself with the code from the authenticator enrolled there, for as long as you chose, at most a year. Those commands run only in a separate Root thread that reads nothing untrusted, never from an agent, and the machine records each one with its output. [The bastion](wiki:the-bastion) page describes the grant. It never puts an AI agent on your machines.

## What else is off limits to the Host?

- **Deleting files in a connected folder.** There is no delete operation for them, and a folder grant cannot include one.
- **Revealing your keys.** Showing a key in clear text takes your code, in the Keys app only, and the Host cannot call it.
- **Your backups and exports.** Restoring, confirming an export, and switching the live copy of your memory on or off are all walled from the Host.
- **A shell on your machines.** The Host cannot open a shell on one of your machines, and cannot even draft a grant for one.
- **Merging as you.** Your Host holds nothing that can merge code under your account. [Approvals](wiki:approvals) covers what else waits for you.

## Why is the rest allowed?

Because none of these walls depends on the model behaving. Each is a check that runs anyway, and each refusal is something you can see. That is what makes it reasonable to let your Host do everything else. [How a Host handles a request](wiki:how-a-host-handles-a-request) shows where each wall sits on the way through.
