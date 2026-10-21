---
title: Agents
summary: An agent on a Host is a narrow, throwaway worker your Host sends to do one piece of untrusted work outside your Host, such as reading the open web or someone else's code. It starts with no permissions, gets only what the task needs, runs in the background and shows in the Agents app. Your Host also runs work on a schedule. Standing jobs run no model at all, and a repeating scheduled turn, which runs a model each time, needs your approval first.
section: host
order: 30
questions:
  - What is an agent on Privatae?
  - Do I have to build or manage agents myself?
  - What is an agent my Host sends out allowed to do?
  - Where can I watch what my agents are doing?
  - Can my Host run jobs on a schedule?
  - How do I stop an agent or a scheduled job?
  - Do agents use up tokens?
---

On a Host, an agent is a worker your Host sends out to do one piece of work for you. You never build one and you never talk to one. You tell your Host what you want, and it writes the agent's brief, decides what the agent may touch, runs it, and reads what comes back. There are dispatched agents, for untrusted work outside your Host: the open web, someone else's code, or a message from another Host or person. Everything inside your Host, such as your projects, apps and settings, your Host does itself. And there is scheduled work, which runs on a clock and often needs no model at all.

## What is a dispatched agent?

It is for untrusted work outside your Host: reading the open web, working through someone else's code, or handling a message from another Host or person. An agent reads that content so your Host does not have to act on it directly. For each one the Host writes the task in plain words, the list of tools the agent may use, and the capabilities it grants. The agent keeps no memory of its own and is gone when the task is done. It runs on the model the Host names for that task, or else the one you or your Host prefer for that kind of work (the web, someone else's code, or a message from another Host or person), or else your Host's own model. A named or preferred model you have ruled out is refused, and no agent starts.

Work inside your Host is never a job for an agent, however many steps it takes: your projects, your repositories, its own code, your apps, settings and network. The Host does that itself. A dispatch that reads nothing from outside your Host is refused, and so is one that gives the agent any tool for work inside your Host, even beside the tools that read from outside; the Host then makes those calls directly, so a model is not paid to retype arguments the Host already holds.

## What is an agent allowed to do?

Nothing, until the Host grants it something. The floor is zero. An agent reads and reports back, so the Host chooses only from the three capabilities that read: reaching outside (the web, someone else's code, a connected server), reading a message in the inbox, and reading your context. A grant that would let an agent change something (your context, your memory, run tools, or dispatch further work) is refused, and so are administering the Host and running a shell. Whatever the agent's report leads to, the Host does itself.

What the agent then acts under is the overlap of two things: the Host's own permissions and the grant. So it can never do more than your Host could, never more than its task allows, and it cannot give itself more, because it never held anything to begin with.

The checks run in code at every call, per operation rather than per tool. An agent allowed to read memory can search it and is refused when it tries to add to it, even through the same tool. A tool the agent was never given is refused outright. A dispatch that could not succeed as written is refused before it starts, so no half-run job is left behind. And no agent can run code: that goes to a coding engine through the Host's own build tools. [What a Host cannot do](wiki:what-a-host-cannot-do) has the full set of walls.

## Where can I watch them?

In the Agents app, which shows everything your Host has running in four lanes: agents it dispatched (a model runs), work on coding engines (builds and comparisons), scheduled turns (a model runs each time), and standing jobs (no model at all). Each dispatched agent shows its state, the model it ran on and where that choice came from, and you can open its transcript as it runs. A call the gate refused is marked in red there, rather than vanishing.

The Agents app only watches. To stop a running agent, press **Kill** on its card in the conversation, or ask your Host.

## Can my Host run jobs on a schedule?

Yes, in two ways on the same clock:

- **A scheduled turn.** At the set time, or as soon as your Host is back if it was down then, the Host runs a prompt through the same governed steps as a live conversation, then reports back into the thread where the job was set. It uses your model each time. A scheduled turn that repeats is set only after you approve it: your Host files a card saying what it will run, how many model turns each run takes and any spending cap, and nothing is set until you choose **Let it run**. Your Host cannot decide that card. A one-time scheduled turn is set without a card. Each task may run at most the number of model turns a day set in your limits (200 unless you change it in [Settings](wiki:settings-app)); a run past that is skipped and spends nothing.
- **A standing job.** One known operation each time it fires, with no model and no tokens. Only operations that finish with nobody present can be scheduled this way.

Jobs can run once or repeat hourly, daily, weekly, monthly or at a set interval. A repeating job that was missed while your Host was down runs once when it is back, then waits for its next time. Because standing jobs are deterministic and run no model, your Host sets them up without asking you first; a standing job whose operation itself sets up a scheduled turn counts as model work and needs your approval like one. That is why every one appears in the Agents app with a **Stop** button, and a job you stop stays stopped. Every Host starts with one: a weekly note about what changed in how it works, which arrives as a Notice on the Support card in Circles.

## Do agents cost tokens?

Dispatched agents and scheduled turns run on your models and your keys, so they use your provider account. Each scheduled run's report says what it cost when it had a price, and the Agents app shows what each scheduled task spent in the last 24 hours. Standing jobs run no model, so the Agents app shows them as "no model" rather than as a cost.
