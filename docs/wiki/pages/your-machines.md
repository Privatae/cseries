---
title: Your machines
summary: Once a machine runs a bastion, Oversight shows the problems it reports, and your Host changes it only through programs it writes and you approve, or other narrow lanes you granted, each checked on the machine.
section: network
order: 60
questions:
  - Can my Host run commands on my machine by itself?
  - How does my Host make changes to my server?
  - What is Oversight?
  - Does my Host need my approval to change my machine?
  - Is there an SSH server or open port involved?
---

Once a machine runs a [bastion](wiki:the-bastion), your Host can watch it and work on it within what you allow. Oversight shows the problems your machines report. Your Host changes a machine only through programs it writes and you approve, or through other narrow lanes you granted, each checked on the machine. Your Host cannot open a shell on your machine, and none of this needs an SSH server or an open port: every lane runs over the connection the bastion opens to your Host.

## What is Oversight?

The Oversight app, which comes with every Host, lists the problems your machines have reported, worst first, with when each was first seen and last seen. The list is built every five minutes, with no model, from what each bastion reports: its alerts, programs that differ from the baseline it recorded, the machine's health, whether it is still checking in, and the machine's own record of findings.

It lists problems only. An empty list means nothing is filed, not that every machine was checked; the Machines view says which machines are actually being watched. The one action is **Mark resolved**, which records that you dealt with a finding. Your Host can read the findings but cannot mark one resolved.

## How does my Host change a machine?

Mainly through programs it writes, called ops. An op declares which files it may change. Before you are asked, it runs as a dry run in a sandbox with no network, and the approval card shows exactly what it would create, change and remove; your Host sees the dry run's result too. You choose **Do it once**, or, for an op that runs as an ordinary user, **Let me do this from now on** until a date, within limits the card states. An op that runs as root asks every time. The machine refuses a run that changes anything outside what the op declared, and a one-time run lands only the changes you saw. Every apply spends an approval from your approval device or the second factor enrolled on that machine. Your Host cannot answer its own approval card. Ops run on Linux machines. Running single commands as root on a machine your Host anchors is separate from ops: it needs a grant you give at the machine itself, which [the bastion](wiki:the-bastion) page describes.

The other lanes are narrower: a fixed list of built-in operations with fixed command lines, model downloads (see [local models](wiki:local-models)), and builds in a container image you name. Each needs a management grant, and a grant that allows anything needs your authenticator code. Without a grant, nothing runs. On a machine anchored to your Host, a change also waits for an approval from your device or at the machine, used once, apart from standing work you set up there and taking access away. Every management action is recorded in the Management audit in the Network app.

## What does my Host see on a machine?

What the grant lets it read: the machine's state, and in the Network app's Agents view, the AI agents and automations running on it and the scheduled jobs that would start one. Observing reads and reports. It changes nothing on the machine, and nothing in the bastion types or clicks for your Host.

The rules for who may do what on your machines are on [network rules](wiki:your-private-network). The [Oversight app](wiki:oversight-app) page describes the window where machine problems show up.
