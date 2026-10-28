---
title: The Context app
summary: Context is the file explorer for your own material, wherever it sits, in your Host's own database, in a bucket you own or on your own machines, kept separate from what your Host remembers. It comes with every Host.
section: apps
order: 10
questions:
  - What is the Context app for?
  - How do I upload files for my Host to use?
  - Does sorting files into collections move them?
  - Can I search my files by meaning?
  - What does the search box in Context search?
  - How do I connect a folder on my own computer?
  - What is the difference between Context and Memory?
app: context-manager
---

Context is the file explorer for your own material: the documents and files you bring in, wherever they physically sit. It is kept separate from your Host's own memory, which you look at in the [Memory app](wiki:memory-app). Context comes with every Host.

## Where can my files live?

In three kinds of place, listed under Sources: your Host's own database (the app labels it Platform), a bucket you own (S3, or anything that speaks like it), and folders on machines of yours that run a [bastion](wiki:the-bastion). Each machine shows whether it is online, and a file held on a machine that is offline says so rather than looking empty.

## Does organising files move them?

No. Collections are labels. One file can sit in several collections whichever machine holds it, and sorting never moves a byte. Some collections are there from the start, such as Documents, Files & media, Conversations, Staged by host and Promoted, and you can make your own.

## What can I add, and what becomes searchable?

Any file: drop it on the window or use Add files. Text, Markdown, JSON (including chat exports) and CSV files are split into pieces and embedded so they can be found by meaning. Pictures, PDFs and other files are stored exactly as they are. A PDF, an Office document, an email, a web page or an archive is also read, and its text is embedded the same way; a file your Host cannot read is found by name or collection. A preview is fetched only when you open the file.

## What does the search box search?

Five places, each under its own heading: your documents (first, grouped by file), your Host's memory, your projects, your code and your Host's skills. A skill shows its name, who wrote it and what it is for, never its text. Search uses meaning and exact words together. Each heading says when meaning search is unavailable, how many weak matches it left out (the same ones your Host's own recall leaves out), or why it could not search at all. What you type is sent in the body of the request, never in a web address.

When a piece should become part of how your Host thinks, **Promote to host memory** copies it in. That is one-way.

## How do I connect a folder or a bucket?

Open the Storage gear, choose Your network, then Add a folder or Add a bucket. That creates a draft, and nothing happens until you approve it. For a bucket, the same approval covers the network rule that lets your Host reach it. Revoking needs no approval and keeps the index. There is no delete here. [Connected folders](wiki:connected-folders) explains grants in full.

A folder's card also shows whether its bastion agent is running, and holds any reorganisation your Host proposes: a list of moves inside that folder, approved by you, tried as a dry run, then applied. "Remove" means moved into an archive folder that you empty yourself.
