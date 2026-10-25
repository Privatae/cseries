---
title: Getting help
summary: Ask your Host and it tells you first where a request from it can go. A C-Series Host cannot send a support ticket to Privatae, so it offers a public GitHub issue you post yourself, or, on the Privatae network, a post on the members' board you approve. A security problem goes to security@privatae.ai.
section: account
order: 30
questions:
  - How do I get support?
  - What information is shared when I report a problem?
  - Is there a community forum?
---

The quickest way to get help is to tell your Host what is wrong. Before it writes anything, it checks what it is connected to and tells you where a request from it can go.

## Can I send a support ticket?

Not from a C-Series Host. Sending needs a connection to Privatae's support service. A standalone Host has no such connection, and the Privatae network does not yet take private support requests from its members, so your Host does not draft a ticket it cannot send. It says so and offers what you can do instead:

- **A Host not on the Privatae network** can write the problem up as an issue on [github.com/Privatae/cseries](https://github.com/Privatae/cseries/issues). That issue is public, so your Host writes it from the problem alone and attaches no memory, conversations or files. It takes out the e-mail addresses, phone numbers, network addresses, links, folder paths, keys and its own name that it can find, but it can miss a name or an address written in plain words, so read the text before you post it. Your Host gives you a link that opens the issue in your own browser, where you see exactly what it says, can change it, and post it yourself. Your Host never posts it.
- **A Host on the Privatae network** can write a post for the members' community board instead. Every member can read the board. The post is written the same way, from the problem alone. Your Host puts it on a card over the chat exactly as it will go, and it is posted only when you press **Post it** there, with a code from your authenticator.
- **A security problem** never goes anywhere public: email security@privatae.ai, as the C-Series security policy asks.

The Support card in Circles says the same. A draft made earlier, before your Host could tell, shows why it cannot be sent and can be discarded.

When the network starts taking private support requests, a C-Series update lets your Host draft and send tickets again, as described below.

## How do I open a support ticket?

Where your Host can send one, tell your Host, in your own words, that something is wrong or that you have a question. It writes the ticket for you: the problem, the context, what it recommends, and anything it would push back on. Tickets are sorted as a bug, a question, a feature request, billing, or access, and can name the project they are about.

The draft waits on the Support card in the [Circles app](wiki:circles-app), and is listed under Waiting on you while it waits. Until you press Send, your Host can correct the draft instead of opening another, and anything you unticked stays unticked. Your Host's tools can draft a ticket but have no way to send one; sending is you pressing Send.

## What gets shared?

The draft lists every item that would go with it, and you can untick any of them:

- the version of your Host and the apps installed on it;
- the project the ticket is about, if it names one;
- what failed in the last three days, grouped and counted, in the words of the provider or tool that failed: calls to your models, your Host's tool calls and its tasks;
- the problems your Host's own check finds in how it is set up;
- a summary of what is running in your Host's container: process names and numbers, no file contents;
- short excerpts of any files your Host named for the ticket;
- for a ticket about a coding run, how that run ended and the last part of its log.

Anything shaped like a secret, such as an API key or a password in a web address, is cut out while the list is put together, before you ever see it. So are the account and request numbers a model provider puts in its error messages.

A ticket carries up to 8,000 characters in each part and up to 12 shared items. Anything longer is marked in the draft at the point where it stops, so you see exactly what goes.

## Is there a community?

Yes, for members of the [Privatae network](wiki:the-privatae-network). The [Community app](wiki:community-app) opens the discussion board every member shares: threads with nested replies, votes on each thread or reply, search, and a button to report a post to the board's moderation queue. Your Host can also read and search the board, send you a summary of new activity, and write a post, which goes out only when you press **Post it** on the card over the chat that shows its exact text.
