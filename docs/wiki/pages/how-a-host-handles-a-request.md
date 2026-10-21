---
title: How a Host handles a request
summary: Your Host reads what you asked and picks a path. It answers you, does the work itself (a read, one known operation with no model involved, or code on a coding engine in its own sandbox), or sends a scoped agent for untrusted work outside your Host. Every path passes the same checks in code.
section: host
order: 20
questions:
  - What happens when I send my Host a message?
  - Does another AI screen my messages before my Host reads them?
  - When does my Host use an agent and when does it just do the thing?
  - Can my Host run code?
  - Which parts of a request are deterministic?
  - What happens if I paste an API key into the chat?
---

Your Host reads your message and decides what kind of work it is. It can answer you, do the work itself (reads, operations, and code through its own coding engines), or send an agent for untrusted work outside your Host, such as the open web or someone else's code. Whichever path it takes, every action passes the same capability checks, written in code rather than in a prompt, and anything that would widen what it may do still stops for you.

## What happens before the model sees my message?

Plain code runs first, with no model involved:

1. **Secrets are cut out.** Text that has the shape of a known secret, such as a provider API key, a private key, an access token or a password inside a link, is removed from your message before it can reach the model or memory. Keys belong in the Keys app.
2. **Tricks are screened.** Text that tries to fake a boundary in the conversation, or tells the Host to ignore its rules, is stripped out and the turn is flagged.
3. **The turn is recorded.** Your message is written to memory, stamped with the moment it arrived.
4. **Context is built.** The Host assembles its instructions, where the conversation stands (its goal and what the last turn left for this one), a window of your recent conversation, your preferences, its notes about people and the rules you taught it. The facts it has gathered, your documents and anything else it remembers, it looks up when the work needs it. This step is deterministic.

A brand-new Host writes its own first-run identity and its permissions in the first moments after it starts. A message that arrives before they are in place waits for them, with a short note saying so, instead of reaching a Host that has no tools. If they are not in place within about ten seconds, the reply says that nothing was done and why. Your message is kept in the conversation, and the Host runs it by itself, once, as soon as its permissions are in place. You do not need to send it again, and if you send something newer in the same conversation first, the Host is shown your earlier request next to the newer message and decides what to do, so nothing you asked is dropped silently.

## Is another AI deciding what I meant?

No. There is no separate model sorting your message before the Host sees it, and no second model vetoing the path it picks. The model your Host runs on reads the request and chooses a path. Governance comes after that choice and it is structural: a capability gate with no model in it checks every call.

## What paths can a request take?

- **An answer.** Conversation needs nothing more.
- **A read.** The Host can read your projects, apps, network, calendar, memory and files directly to answer you. Reading changes nothing.
- **One known operation.** When the Host already knows the exact action and its arguments, such as revoking a network rule, restarting an app, installing one from the store, switching a model or scheduling a task, it makes that one call itself, with no agent and no model between the decision and the action. The list of operations allowed to run this way is closed, and every tool inside your Host is on it or is a read; a tool nobody has classified fails a check before it ships, rather than being handed to an agent. The one addition is a tool on an MCP server you have connected and approved in the Network app, which runs this way too, behind the same checks. Everything inside your Host, including your projects and repositories, its own code, your apps, settings and network, the Host does itself.
- **A command on your machine.** On a machine it anchors, if you allowed it at the machine itself, the Host can run commands there as root, one at a time, in a separate thread called Root that reads nothing untrusted: no web, no messages from others, no memory and no agents.
- **A dispatched agent.** For untrusted work outside your Host: reading the open web, someone else's code, or a message from another Host or person. The Host writes the agent a brief, picks its tools, and grants it only the capabilities the task needs, so nothing it reads can act on your Host. Work inside your Host, however many steps it takes, the Host does itself. The agent starts with nothing, works in the background while you keep talking, and hands its result back to the Host. [Agents](wiki:agents) covers this.
- **Code.** Building or changing software goes to a coding engine you chose, in a disposable sandbox beside the Host, on your own key or sign-in, through the Host's own build tools. Neither an agent nor a known operation can run code. [Coding agents](wiki:coding-agents) covers the engines.

When the Host sends an agent off, its reply tells you what each agent is doing, so you can see at once if it misread you.

The Host is asked to end each turn with a short note in a fixed shape: what kind of turn it was, which steps helped and which did nothing, what would have made it faster, and what the next turn needs. The note is not hidden: when it writes one, **review**, under the reply, shows it.

## Which parts are deterministic?

Everything except the reading and the writing. The screens, the recording, the context assembly, the known operations, the capability checks and the gates that wait for your code are all plain code that runs the same way every time. So are standing jobs, the ones your Host runs on a schedule with no model at all. The model's part is to understand what you asked, choose the path, write an agent's brief when one is needed, and compose the reply.

## What does that look like in practice?

- *Restart the calendar app.* One known operation. One call, the same gates, no agent.
- *Look into these three suppliers and bring back a comparison.* Work on the open web, which is untrusted, so the Host can send agents, one per supplier, each allowed to read and reach the web and nothing else. If the Host then wants to act on what they found, it does that itself, as a separate step.
- *Fix the bug in my project.* Inside your Host, so the Host reads the code itself and sends the change to a coding engine through its own build tools, with no agent.
- *Add a dark mode to my notes app.* Code, so it goes to a coding engine and never to an agent.

Four requests, three paths, one Host. [What a Host cannot do](wiki:what-a-host-cannot-do) describes the walls between the paths, and [Approvals](wiki:approvals) lists what always waits for you.
