---
title: Settings
summary: Settings holds your Host's name and version, its appearance and time zone, how it responds, the limits on what it may spend and how long its work may run, the layout of your desktop and dock, its backups, and the optional Privatae network membership. It comes with every Host and cannot be removed.
section: apps
order: 10
questions:
  - What can I change in the Settings app?
  - How do I rename my Host?
  - How do I update my Host?
  - How do I join the Privatae network?
  - How do I change my desktop wallpaper or time zone?
  - How do I change how much my Host explains, or its personality?
  - How do I change what my Host may spend, or how long its work may run?
  - Where do I set up backups for my Host?
  - Can my Host restore or change its own backups?
app: settings
---

Settings is where you look after the Host itself: its name and version, how the desktop looks, which apps sit in the dock, and its backups. It comes with every Host, and it is one of the few apps that cannot be uninstalled.

## How do I rename my Host?

About has a **Name** field: type the name and choose Save. Only the owner, signed in, can change it.

## How do I update my Host?

The Settings app shows the version your Host is running under About. Updating is done on the machine with `npx cseries update`; see [updates and versions](wiki:updates-and-versions). On a Host on its own, **Check for updates** in the Updates tab looks up the newest published release of the installer and either says you are up to date or tells you to run `npx cseries update` on the machine. If it cannot find out, it says why.

## What can I change about how it looks?

Appearance has the desktop wallpaper, a separate wallpaper for phones and other small screens, your own image, and the time zone. Your Host speaks every time and date in that zone; without one it uses UTC. Once your Host has designed a desktop for you, it appears here as a Host design swatch, and choosing any other swatch puts an ordinary wallpaper back. Choosing the Host design checks that its newest version displays properly first. If the newest edit does not, your desktop keeps showing the last version that did, and Settings tells you so.

Desktop and Dock is where you choose which apps sit in the dock and on the desktop, and reset the layout.

## How do I change how my Host responds?

Response Shaping has two settings. **How much your Host explains** has three positions: explain everything, explain new things, or use the terms. You can also ask your Host in conversation, for example "less detail", and it moves this same setting. **Personality** is ten sliders, such as warmth and directness, each with a line that says how your Host will sound at that position. The sliders are yours and never move on their own: your Host does not move them, and Settings shows you every slider you changed before it saves them.

## What are Limits?

The Limits tab lists every limit that can stop work you asked for: what one reply or one scheduled turn may spend, how many turns a scheduled task may run in a day, how many repairs a build starts by itself, how long a build, a browser agent or a query on your Host's own data may run, and the rest. Each starts at a default and is yours to change; 0 means no limit, and you can put the default back at any time. Each one says what happens when it is reached and when it last stopped something.

When a limit stops work, your Host is told which one and its value, and what was kept. If the work needed more, your Host can ask you to raise it: a card shows the value it recommends and why, and nothing changes unless you choose it. Your Host can read every limit but can never change one.

## How do backups work?

The Backup tab sets a schedule (every day, once a week, or off), where backups go, and holds your keys. Every backup is encrypted on your Host, to the recovery key you make in the Backups app and to the key of any bastion you add, so nobody else can open it; until your Host has a recovery key it takes no backups. A Host on its own keeps them in a folder on your machine unless you choose your own S3-compatible bucket; on the optional Privatae network they can go to the network's storage. A backup holds your Host's whole state: its database (memory and conversations), its apps, files and settings, each app's own data, including data you kept from an app you removed, and copies of its Git repositories. Restore puts all of it back except the Git repositories, which stay as they are.

## How do I join the Privatae network?

The Network tab takes the one-time code from your Privatae account page. It is optional; see [joining the Privatae network](wiki:the-privatae-network).

## Can my Host change these for me?

Backup keys, restoring, sending a copy and switching the memory mirror on or off are yours alone: your Host is walled off from all of them.
