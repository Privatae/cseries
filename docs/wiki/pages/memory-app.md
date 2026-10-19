---
title: The Memory app
summary: Memory is your window into what your Host remembers about you, with every correction it has made and a way to see what it believed on any past date. It is built into every Host and cannot be removed.
section: apps
order: 10
questions:
  - How do I see what my Host remembers about me?
  - Can I correct something my Host remembers wrongly?
  - Can I see what my Host believed last month?
  - Can I delete a memory?
  - How do I know my Host's memory is still working?
  - What does my Host keep in its own database?
  - Can I undo a change my Host made to its own tables?
app: oracle
---

Memory is your window into what your Host remembers about you. It is built into every Host and cannot be removed. New files come in through the [Context app](wiki:context-app); this is where you read, correct and manage what your Host has kept.

## What can I see?

Five views:

- **Ledger**: the entries your Host keeps, such as your preferences, notes about people in your life, the goal of each conversation, cited facts and research, which you can filter and search. A rule you taught, or one your Host learned, is a preference marked as a taught rule or a learned rule, and nothing automatic retires it. Search tells you when it could match only on words because meaning search was unavailable.
- **Threads**: memory grouped by where it happened, such as a project, an app or another Host. A thread splits into separate conversations wherever it went quiet for half an hour.
- **Corrections**: every entry your Host changed its mind about, showing what it held, when that stopped being true, and what replaced it.
- **Health**: how memory is forming. Your Host writes what it learns as it answers, so there is no background pass to wait on.
- **Data**: what your Host keeps in its own database. When you tell it something with a shape of its own, such as the tools in your kitchen or a habit you are building, it can keep that in tables it designs for you. Each table, view and function says what it is for. You can read any table's rows here, and nothing you look at can change them.

## Can I undo a change my Host made to its own tables?

Yes. Before any change that could remove or reshape what it keeps, your Host takes a snapshot. The Data view lists them, newest first. Restore puts everything back as it was at that snapshot, and anything made after it is removed. A snapshot of how things are now is taken first, so a restore can be undone too.

## Can I see what my Host believed on a past date?

Yes. The picker at the top has three settings: **Canonical** shows what your Host holds now, **As-of** takes a date and shows memory exactly as it stood then, and **All** shows every version including the replaced ones.

## How do I correct or remove an entry?

Open it and choose **Correct**. The original is not overwritten: it is closed with the moment it stopped being true, linked to the new version, and kept in the history. If the new text holds something shaped like an access key, a token, a private key or a password in a web address, that part is replaced with a marker before it is kept. **Mark resolved** closes a finished goal. **Archive** takes an entry out of use. **Delete** removes an entry from your Host's memory for good, and it is the one action here that takes something out of the history; earlier backups keep their copy until they age out.

[Memory](wiki:memory) explains how your Host decides what to keep.
