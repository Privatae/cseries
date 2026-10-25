---
title: Updates and versions
summary: You update a Host by running the installer's update command on the machine. The containers are recreated from the published release and attached to the same volumes again, so your conversations, memory, apps, projects and files stay.
section: host
order: 80
questions:
  - How do I update my Host?
  - Where can I see which version my Host is running?
  - Will an update delete my conversations or files?
  - Can I pick a version or roll back?
  - Do apps update separately from the Host?
  - Does the bastion on my computer update itself?
---

A Host moves to a new version when its containers are replaced by the published release. They are recreated from the new images and the same volumes are attached again, so your conversations, memory, apps, projects and files carry straight over.

## How do I update my Host?

On the machine, run `npx cseries update`. It checks the machine, pulls the published images and starts the Host on them, the same steps as a first install, without opening the setup page. `npx cseries status` shows whether the Host is running.

If you built the images yourself from the source, rebuild them and run `docker compose up -d --build`, as the guide in the C-Series source repository describes.

## What changes, and what stays?

**What changes:** the Host's own software and the apps built into it, and the database container's software.

**What stays:** everything on the volumes. One holds the database, meaning every conversation and everything your Host remembers. The others hold your installed apps and their data, your projects, your files and the Host's settings and secrets.

[What a Host is](wiki:what-a-host-is) describes the containers and their volumes.

## Where can I see my version?

In the Settings app, the About tab shows the version your Host is running and when that version was built.

## How do I know a new version is out?

In the Settings app, open Updates and choose **Check for updates**. On a Host on its own it looks up the newest release of the installer and compares it with the version you run: it says "You're up to date" with your version, or names the new version and tells you to run `npx cseries update` on the machine the Host is installed on. If the check cannot reach the release, it shows the reason instead.

## Can I pick a version or roll back?

The installer's update always moves to the release it publishes. It has no command to go back to an earlier one.

## Do apps update separately?

The apps built into the Host change only when the Host itself updates. Apps you installed from the [App Store](wiki:app-store-app) update separately, with no membership needed: your Host updates them each time it starts, and the App Store shows an Update button when a newer version is waiting. A new version that would reserve more of the machine's memory than the one you have waits for you to update it there.

## Does my Host tell me what changed?

Every Host is set up with a weekly note, on the Support card in Circles, about what changed in how it works: rules it has learned from your corrections, how its coding runs went, how its memory is forming, and what is not set up yet. It runs with no model, and you can stop it from the Agents app. It reports on your Host's behaviour, not on software versions.

## Does the bastion update itself?

The bastion is a separate program with its own update path; [the bastion](wiki:the-bastion) covers it.
