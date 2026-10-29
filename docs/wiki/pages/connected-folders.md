---
title: Connected folders
summary: A connected folder is a folder on one of your own machines that your Host can search and read without the files being uploaded or synced. You grant it, your machine checks every request against that grant, and there is no way to delete a file through it.
section: network
order: 30
questions:
  - Can my Host read files on my computer without uploading them?
  - How do I give my Host access to a folder?
  - Can my Host delete my files?
  - What leaves my computer when my Host searches a folder?
  - Where is the index of my folder kept?
---

A connected folder is a folder on one of your own machines that your Host can search and read where it sits. Nothing is uploaded and nothing is synced. You grant the folder, the machine holding it checks every request against that grant, and there is no way to delete a file through it.

## How do I connect a folder?

Use **Add a folder** under Your network in the Context app, have the machine's bastion propose the folder, or ask your Host. Your Host drafts a grant: the folder, the paths left out, and what may be done there. The default is read, search and index. The paths left out start from a list of secret patterns, such as SSH and GPG folders, cloud credential folders, `.env` files, private keys, tokens and shell histories, plus the `.agent` folder where the bastion's own agent keeps its data. Ask your Host for a different list when it drafts the grant.

Connecting a folder always widens access, so it always waits for you. You approve it with a tap on your approval device if you pinned one to that machine, with a code typed at the machine if it has a second factor of its own, and otherwise with your authenticator code. A machine anchored to a Host refuses the grant if it has neither a pinned device nor a second factor of its own. Once approved, the grant is signed with your Host's key and sent to the machine, which checks that signature again on every request.

A grant lasts a year by default.

## Can my Host delete my files?

No. A grant is written from a fixed list of words and delete is not one of them; the database refuses a grant that contains it. The only change your Host can make is a plan to reorganize the folder: an ordered list of moves inside it. Tidying a file away means moving it into an archive folder, which you empty by hand. You approve the plan, and an approved plan expires after a day. A dry run checks the plan on the machine without touching a file.

## What does my machine keep?

When a grant includes search and index, the folder is indexed on the machine that holds it, and searches are answered there. Search needs Python 3.8 or later and an embedding model running in Ollama on that machine; the Context app says when Python is missing.

## What crosses to my Host?

- **A listing.** File names, sizes and modification times.
- **A search.** Your question is matched on your machine, and the matching text comes back.
- **A read.** One range of one file, up to 512 KiB.

Every answer comes back as one of four states: the content, offline (the machine cannot be reached), refused (with the reason), or gone (the file is not there). An empty answer is never left to mean either nothing or a failure.

## Who checks each request?

Both ends. Your Host refuses any operation without a signed grant that lists it. Your machine checks again, on your hardware, before a byte is read: the grant's signature, the operation, and whether the path is inside the folder and not excluded. Your Host also records every request it sends, allowed or refused.

The [Context app](wiki:context-app) shows your connected folders beside everything else your Host can reach, and sorting files into collections there never moves a byte. How a machine is paired is on [the bastion](wiki:the-bastion) page.
