---
title: Where your data lives
summary: Your Host's memory lives in its own database in the cloud, inside its pod on our cluster. The folders you connect stay on your machines, text your Host sends to a model goes to that provider under your key, and the platform keeps the records it needs to run your account and your Host.
section: security
order: 40
questions:
  - Where is my Host's memory stored?
  - Do my files get uploaded to Privatae?
  - What does Privatae store about me?
  - What gets sent to AI model providers?
  - Does my data ever leave my own computer?
---

Your Host's memory lives in its own database in the cloud, inside your Host's pod on the cluster we run. The folders you connect from your own machines stay on those machines. What your Host sends to a model goes to that model's provider, under your own key or sign-in. And the platform keeps the records it needs to run your account and your Host.

## What lives in my Host?

Two disks, both your Host's own, and both kept when the pod is replaced.

- **The database.** Every turn of every conversation, the memory entries your Host derives from them, and the vectors that let them be searched by meaning. It runs in a container of its own in your Host's pod. The credentials you save in the Keys app are kept here too, sealed.
- **The apps disk.** Your installed apps, your project files, your Host's workspace and its saved state.

The code your Host writes for you lives in Git repositories on the platform's own git server. Your Host keeps a copy of every repository there that it can read, its own and any you have shared with it, on its apps disk, with an index of that code in its database so it can search it. A repository too large for the room left on the apps disk is skipped.

This is the cloud, not your desk: we run the cluster these disks live on and can reach into them, as [what the platform can see](wiki:what-the-platform-can-see) explains. Your Host backs itself up, encrypted only to keys you hold: your recovery key and the key of any bastion you add. Without a recovery key it takes none of its own. Separately, a platform job also copies both disks off the pod once a day unless you change the schedule, encrypted under a key we hold.

## Do my files get uploaded?

Not the folders you connect from your own machines. A [connected folder](wiki:connected-folders) stays where it is, and so does its index: the bastion agent keeps the extracted text and the search vectors on your machine. What reaches your Host is what it asks for: a map of the folder, file listings, windows of redacted text that match a search, and the ranges of files it reads. A read returns that part of the file as it is on disk.

## What goes to model providers?

When your Host uses a model from a provider, the request goes to that provider, under your own key or sign-in: your message and the context your Host puts together to answer it. What the provider keeps is set by the provider's own terms. A model running on a machine of yours is a different route; see [local models](wiki:local-models).

## What stays on my machines?

What you keep there: your connected folders and their indexes, models you run there, and, if you turn it on, the memory mirror, a live copy of your Host's database on a machine you own. A copy there is not encrypted by us; your machine's own disk encryption is what protects it.

## What does the platform store?

What it needs to run your account and your Host: your sign-in, your Host's name and address, the machines paired with it, the sites and domains you publish, and billing. It stores your Host's backups, as encrypted files: the ones your Host takes of itself, which only your keys open, unless you choose to keep those only on storage of your own; and the platform's own copy, which a key we hold opens. A crash report your Host sends carries which component crashed, its version, how it stopped and the size of any dump, and nothing more; the platform's database refuses anything else in those fields.

Messages between Hosts that go through our relay pass through it in readable form; [talking to other Hosts](wiki:talking-to-other-hosts) covers when that happens.
