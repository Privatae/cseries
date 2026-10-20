---
title: How a Host remembers
summary: Your Host writes down every turn of every conversation in its own database, notes what is worth keeping as it answers, and when something turns out to be wrong it closes the old entry and replaces it rather than erasing it. The Memory app shows all of it, including what your Host believed on a past date.
section: host
order: 40
questions:
  - Does my Host remember our past conversations?
  - How does my Host decide what to remember?
  - How do I correct something my Host remembers wrongly?
  - Can I make my Host remember something word for word?
  - Where is my Host's memory stored?
  - Can I see what my Host believed last month?
  - Does memory need an embedding model?
---

Your Host remembers by writing things down. Every turn is stored in its own database as it happens. As it answers, your Host notes what is worth keeping: what you want, what the conversation is trying to reach, and where the next turn picks up. When something turns out to be wrong, the old entry is closed and replaced rather than erased. The [Memory app](wiki:memory-app) shows all of it.

## What gets written down?

Every turn: what was said, who said it, and the moment it was said. No language model runs to record a turn. It is stored, and turned into a vector for search by meaning if you have set that up, and that is all. Each turn belongs to the place it happened, whether a project, an app or a conversation with another Host, so your Host reads back the thread it is standing in rather than everything you have ever said. A reply that waits for work your Host started is stored before it waits, as it stands then, and completed in place when the turn ends. Once a turn has ended, it is never rewritten.

## How does it decide what to keep?

Your Host is asked to end each reply with a short note in a fixed shape: what you said you want, the goal of this conversation, and what the next turn needs. When it writes one, you can see it under the reply, behind **review**. Nothing reads your conversations again afterwards, and no second model judges what it keeps.

- Something new is added, with the reason and the place it applies.
- Something it already holds, said again, changes nothing, or widens it when you meant it everywhere.
- A new wording of something it holds replaces the old one when the new one applies everywhere the old one did. Both stay in the record, the old one marked with what replaced it. An exception for one place, such as a single conversation, is kept beside the rule it narrows.

Your Host can also keep a note about someone in your life, and a cited fact: when research turns up something worth keeping, what the source said, in the source's own words, with where it came from and the date it was read. When that source is read again and the words are gone, the cited fact is marked stale. Other facts, decisions and passing details are not kept as separate memories. The conversation itself is, word for word, and can be searched.

## Can I make it remember something word for word?

Yes. Start a message with "Remember this:", "Always remember:" or "Never forget:", and what follows is stored exactly as you wrote it the moment you send it. Nothing merges it, fades it or replaces it on its own; it changes only when you ask your Host to change or forget it.

## How does memory get used when I ask something?

Your message is itself the search. Before the model is called, your preferences and notes about people that match what you just said are placed in front of it, and so is one line for each of up to two of your Host's own [skills](wiki:skills-app) that match it. A preference stays where it belongs. It starts out used only where it was said: in that project, app or conversation. Your Host widens one about you or how you like work done so it applies everywhere, and keeps one about a project or an app, such as how your finance app sorts bills, only there. Your documents, cited facts, research, the tables your Host keeps for you and past turns are not put in front of it unasked. Your Host finds those by searching on purpose: one search across its memory and your documents, with results grouped by source, and a cited fact comes back as a quote with the date it was read, never as something your Host believes. If meaning search is not available, the result says the search matched on words only, and a source that could not be reached is named as unavailable rather than reported as empty.

Each turn carries the recent turns word for word and, after them, where the conversation stands: its goal and what the last turn handed on. In Circles, once a conversation grows past what fits, the turns that no longer fit are carried as a short summary ahead of the recent ones, rewritten after a turn ends. Every turn stays in the record word for word, and your Host searches them when it needs them.

## How do I correct it?

Tell it, in the conversation, the way you would tell a person. Your Host replaces what it held: the earlier entry closed, the new one live, the link between them kept.

You can also do it yourself in the Memory app. Correcting an entry closes the original, stamped with the moment it stopped being true, and writes a new version linked to it. You can also mark a goal resolved, archive an entry, or delete one. Deleting is the one action that removes an entry from the record for good.

## Can I see what it believed on a past date?

Yes. The Memory app has a picker with three settings: **Canonical** for what your Host holds now, **All** for every version including replaced ones, and **As-of** to pick a date and see memory exactly as it stood that day.

## Where is my Host's memory stored?

In your Host's own database, which runs in a container of its own on your machine, on a volume that only that container mounts. A Host on its own sends none of it to Privatae. In this release a standalone Host cannot yet write its own backups, so keep a copy of the volume yourself if the memory matters to you; you can also keep a live copy of the database on a machine of your own through the bastion.

## Does memory need an embedding model?

Search by meaning does. Your Host embeds text with a model you choose, either one running on a machine of your own or one from a provider you hold a key for, and it is set up in the Orchestrator app. Until one is set up, memory still works and search matches on words. Switching to a different embedding model later keeps the vectors already made. [Models and keys](wiki:models-and-keys) and [local models](wiki:local-models) cover the choices.
