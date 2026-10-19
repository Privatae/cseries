---
title: Settings
summary: Settings holds your Host's version and updates, its appearance and time zone, how it responds, the layout of your desktop and dock, and its backups. It comes with every Host and cannot be removed.
section: apps
order: 10
questions:
  - What can I change in the Settings app?
  - How do I update my Host?
  - How do I change my desktop wallpaper or time zone?
  - How do I change how much my Host explains, or its personality?
  - Where do I set up backups for my Host?
  - Can my Host restore or change its own backups?
app: settings
---

Settings is where you look after the Host itself: its version and updates, how the desktop looks, which apps sit in the dock, and its backups. It comes with every Host, and it is one of the few apps that cannot be uninstalled.

## How do I update my Host?

The Updates tab shows the version your Host is running and checks for a newer one. When one is available you apply it from there, and History keeps a record of every update, including any that failed and why. [Updates and versions](wiki:updates-and-versions) explains how releases reach a Host.

## What can I change about how it looks?

Appearance has the desktop wallpaper, a separate wallpaper for phones and other small screens, your own image, and the time zone. Your Host speaks every time and date in that zone; without one it uses UTC. Once your Host has designed a desktop for you, it appears here as a Host design swatch, and choosing any other swatch puts an ordinary wallpaper back. Choosing the Host design checks that its newest version displays properly first. If the newest edit does not, your desktop keeps showing the last version that did, and Settings tells you so.

Desktop and Dock is where you choose which apps sit in the dock and on the desktop, and reset the layout.

## How do I change how my Host responds?

Response Shaping has two settings. **How much your Host explains** has three positions: explain everything, explain new things, or use the terms. You can also ask your Host in conversation, for example "less detail", and it moves this same setting. **Personality** is ten sliders, such as warmth and directness, each with a line that says how your Host will sound at that position. The sliders are yours and never move on their own: your Host does not move them, and Settings shows you every slider you changed before it saves them.

## How do backups work?

Your Host backs itself up once a day unless you change the schedule in the Backup tab: its memory and data, its apps and files, and the code in its own Git repositories. Each backup is encrypted on your Host before it leaves, to your Host recovery key and to the key of any bastion you add, and to nothing else, so Privatae holds no key that opens it. You make your recovery key in your browser on your account page and keep it yourself; without one your Host takes no backups of its own, because there is nobody to encrypt them to.

Separately, the platform also keeps its own copy of every Host on the same schedule, under a key the platform holds. [What the platform can see](wiki:what-the-platform-can-see) covers what that key means.

## Can my Host change these for me?

Backup keys, restoring, sending a copy and switching the memory mirror on or off are yours alone: your Host is walled off from all of them.
