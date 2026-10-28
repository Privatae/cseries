---
title: Where your data lives
summary: Your Host's memory lives in its own database on your machine, and its apps and files on volumes beside it. Connected folders stay where they are. Text your Host sends to a model goes to that provider under your key. A Host with no network membership sends nothing of yours to Privatae, unless you connect it to Privatae's git server.
section: security
order: 40
questions:
  - Where is my Host's memory stored?
  - Do my files get uploaded anywhere?
  - Does Privatae store anything about me or my Host?
  - What gets sent to AI model providers?
  - Does my data ever leave my own computer?
---

Your Host's memory lives in its own PostgreSQL database, in a container on your machine. Its apps and files live on volumes beside it. Folders you connect stay where they are. What your Host sends to a model goes to that model's provider, under your own key. A Host that has not joined the Privatae network sends nothing of yours to Privatae, unless you connect it to Privatae's git server.

## What lives in my Host?

The Host's containers keep their data on named volumes that survive when the containers are replaced.

- **The database.** Every turn of every conversation, what your Host keeps in memory, and the vectors that let it be searched by meaning. The credentials you save in the Keys app are kept here too, sealed.
- **The other volumes.** Your installed apps, your project files, your Host's workspace and its saved state.

All of it sits on your machine: with the installer, inside the Linux virtual machine it makes. Removing the Host with `npx cseries uninstall` keeps these volumes; `npx cseries uninstall --delete-data` deletes them for good.

## Do my files get uploaded?

Not the folders you connect from your machine. A [connected folder](wiki:connected-folders) stays where it is, and so does its index: the bastion agent keeps the extracted text and the search vectors on your machine. What reaches your Host is what it asks for: a map of the folder, file listings, windows of redacted text that match a search, and the ranges of files it reads. A read returns that part of the file as it is on disk.

## What goes to model providers?

When your Host uses a model from a provider, the request goes from your Host straight to that provider, under your own key: your message and the context your Host puts together to answer it. What the provider keeps is set by the provider's own terms. A model running on your machine is a different route; see [local models](wiki:local-models).

## What does Privatae receive?

On its own, a Host sends nothing of yours to Privatae; it only reads which bastion version Privatae has published. The exception is your code, if you connect your Host to Privatae's git server. If you [join the Privatae network](wiki:the-privatae-network), your Host keeps one outbound connection that carries only the network's own requests: the App Store, the community board, group listings, renewing or revoking the passport, a heartbeat, the interests you list for introductions, turning on remote access and the tickets for its tunnels, and requests about your Host's own backups, such as whether the network keeps them and how each backup ended; the encrypted backups themselves go only once the network keeps them, and not if you chose a folder or a bucket instead. If you turn on remote access, your Host also keeps a tunnel open to the network's relays, which pass your browser's encrypted connection through to your Host. Your Host's memory, conversations, files and keys are not sent on their own, and a backup arrives already encrypted to keys the network does not hold.

## How are backups handled?

Your Host backs itself up on the schedule in [Settings](wiki:settings-app), Backup: its database, its apps, files and settings, each app's own data and copies of its Git repositories, as one file encrypted on the Host to the recovery key you make in the Backups app and to any bastion you add. A Host on its own keeps the backups in a folder inside its virtual machine, or in your own S3-compatible bucket if you choose one; nothing goes to Privatae. A Host on the Privatae network does the same until the network answers that it keeps backups; from then on they go to the network's storage, unless you saved the folder or a bucket in Settings, Backup. A folder on the same machine does not protect against losing the machine, so download a backup now and then, or choose a bucket.
