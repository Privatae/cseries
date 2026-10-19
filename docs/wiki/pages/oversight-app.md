---
title: Oversight
summary: Oversight lists the problems your own machines have reported to your Host, worst first, with when each one was first and last seen. It comes with every Host.
section: apps
order: 10
questions:
  - What is the Oversight app?
  - How do I see what is wrong with my machines?
  - How long has a problem on my machine been going on?
  - What does Mark resolved do in Oversight?
  - Does an empty Oversight list mean my machines are fine?
app: oversight
---

Oversight is the list of problems your machines have reported to your Host: one row per problem, worst first, each with the time it was first seen and the time it was last seen. It comes with every Host, so there is nothing to install.

## Where do the findings come from?

From the machines you have connected with the [bastion](wiki:the-bastion). Every five minutes your Host folds what each machine reports into the list: its alert source, what its detection found, checks on how the machine is set up, whether it has stopped checking in, and the findings record the machine keeps for itself. No AI model takes part in this pass. When a machine goes from no critical problems to at least one, the pass runs straight away.

The pass is a standing job, listed in the [Agents app](wiki:agents-app) with a Stop button. If you stop it, a restart of your Host does not turn it back on.

## Why keep the first time a problem was seen?

A machine's report says what is true right now, and the next one replaces it. Oversight keeps the moment a problem first appeared, so you can tell something that started this morning from something wrong all week. A problem that keeps being reported stays one row.

## What does Mark resolved do?

It records that you dealt with a finding, and keeps the row. It is the only change you can make in this window, and it is yours alone: your Host can read the findings but cannot mark one resolved. If the machine reports the same problem again, the finding opens again.

Findings are almost never closed on their own. When a machine stops reporting a problem, the usual reason is that it stopped reporting, so the row stays open. The one exception is an alert that an online machine's alert source no longer lists.

## Does an empty list mean everything is fine?

No. The list only ever holds problems, so an empty list means nothing has been filed, not that every machine was checked. The Machines view shows who is actually being watched: a machine counts only when it is online and its alert source is answering. A machine whose alert source is not set up, has gone quiet, or is offline is one nobody is watching at the moment.

See [Your machines](wiki:your-machines) for how machines join your network.
