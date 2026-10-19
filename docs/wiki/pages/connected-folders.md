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
  - Is personal information removed before my Host sees it?
  - Where is the index of my folder kept?
---

A connected folder is a folder on one of your own machines that your Host can search and read where it sits. Nothing is uploaded and nothing is synced. You grant the folder, the machine holding it checks every request against that grant, and there is no way to delete a file through it.

## How do I connect a folder?

Run `privatae-bastion add-folder <path>` on the machine, use **Add a folder** under Your network in the Context app, or ask your Host. Your Host drafts a grant: the folder, the paths left out, how often the index refreshes, and what may be done there. The default is read, search and index. The paths left out start from a list of secret patterns, such as SSH and GPG folders, cloud credential folders, `.env` files, private keys, tokens and shell histories. Ask your Host for a different list when it drafts the grant.

Connecting a folder always widens access, so it always waits for you. You approve it with a tap on your approval device if you pinned one to that machine, with a code typed at the machine if it has a second factor of its own, and otherwise with your authenticator code. A machine anchored to a Host that has neither a device nor a factor refuses the grant. Once approved, the grant is signed with your Host's key and sent to the machine, which checks that signature again on every request.

A grant lasts a year by default. Revoking one is free, needs no code, and keeps the map of what was there.

## Can my Host delete my files?

No. A grant is written from a fixed list of words and delete is not one of them; the database refuses a grant that contains it. The only change your Host can make is a plan to reorganize the folder: an ordered list of moves inside it. Tidying a file away means moving it into an archive folder, which you empty by hand. You approve the plan with your code, or at the machine if it has its own second factor, and it expires after a day. Before touching anything, the machine refuses the whole plan if a destination already exists, a source has gone, or a path is outside the folder or excluded. A dry run reports whether the plan is ready without touching a file. Applied, the plan undoes its own moves if one fails part way, and it runs only once.

## What does my machine keep?

When a grant includes search, the bastion installs a bastion agent for that folder, after checking the download against the published manifest. The agent runs no language model. It walks the folder, extracts the text, replaces the details its rules recognize with markers, and builds a search index with an embedding model on your machine. Search needs Python 3.8 or later on the machine; the Context app says when something is missing.

Search needs an embedding model running in Ollama on that machine. The agent keeps its working files, the extracted text (before any redaction) and the index, in a hidden folder it creates inside the folder you granted, or in the bastion's own folder if the granted folder is read-only. The file that could turn the markers back into your details is kept somewhere else, in the bastion's own state folder, in case the granted folder is synced to a cloud.

## What does the redaction catch?

Email addresses, phone numbers, US Social Security numbers, card numbers, IBANs and US routing numbers, legacy Bitcoin addresses, IP and MAC addresses, API keys and tokens from common services, and private keys. Names, dates and street addresses are not caught; the rules do not look for them.

## What crosses the tunnel to my Host?

- **The map.** Cards for each folder (how many files and bytes, the newest change, the mix of types, keywords, how many details were redacted) and for each file (a title, the first 300 characters of its redacted text, and keywords).
- **A listing.** File names, sizes and modification times.
- **A search.** Your question is turned into numbers on your machine and matched there. A few windows of redacted text come back, each with a document id your Host can cite, and your Host is told to keep the markers as they are.
- **A read.** One range of one file, up to 512 KiB, exactly as it is on disk, with nothing redacted.

Every answer comes back as one of four states: the content, offline (the machine cannot be reached), refused (with the reason), or gone (the file is not there). An empty answer is never left to mean either nothing or a failure.

## Who checks each request?

Both ends. Your Host's pod refuses any operation without a signed grant that lists it. Your machine checks again, on your hardware, before a byte is read: the signature against the key it pinned, the operation, and whether the path is inside the folder and not excluded. The machine writes each request, allowed or refused, as a line in a log in the bastion's state folder: the operation, the folder, the path and the decision.

The [Context app](wiki:context-app) shows your connected folders beside everything else your Host can reach, and sorting files into collections there never moves a byte. How a machine joins your network is on [the bastion](wiki:the-bastion) page.
