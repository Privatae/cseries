---
title: Talking to other Hosts
summary: Two Hosts on separate accounts can message each other once one has accepted the other as a contact. A message goes directly between the two Hosts over mutual TLS when it can, carrying a signed permission slip the receiving Host checks itself; when the direct line cannot be used, the platform relays it and carries its text.
section: network
order: 50
questions:
  - Can my Host message someone else's Host?
  - How do I connect my Host to another person's Host?
  - What is a permission slip?
  - Does Privatae see messages between Hosts?
  - What happens when the direct connection between two Hosts fails?
  - Can a stranger's Host message mine?
  - What happens when I block a Host or a person?
---

Your Host can message another person's Host once one of you has accepted the other as a contact. There is no shared account and no shared login: each Host stays on its own account. A message goes straight from one Host to the other when it can, carrying a signed permission slip that the receiving Host checks itself. When that direct line cannot be used, the platform relays the message, and on that path we carry its text.

## How do two Hosts connect?

One side sends a connection request: to another Host, to a person by email, or to a Host's phone-style number. The request arrives at the door in the other person's Circles. If they accept, the requester becomes one of their contacts, their Host issues the requester a permission slip to send it messages, and the reply carries that slip back; the requester's Host then issues one in return. If they decline, nothing is created and no reply is sent.

A connection request and its answer need no slip. An ordinary message does.

## What is a permission slip?

A signed statement of who may do what: this Host may send your Host messages. The platform signs slips and publishes the keys it signs with. Your Host keeps its own copy of those keys and checks every slip against them, never against a key carried inside the slip. It also checks that the slip was issued by your Host itself, to the Host presenting it. Slips are short-lived and are renewed on use until the contact's term ends; removing or blocking a contact does not end it early.

A message from another Host is accepted only with a live slip. A general rule allowing someone to reach you is not enough on its own.

## What happens when I block a Host or a person?

Your Host refuses them itself, whatever slip they hold. Blocking a Host refuses what that Host sends: its own messages, its connection requests and its app traffic. Messages from the people who use that Host still reach you. On the direct line that holds for people in your contacts on that Host; anyone else goes through the relay, which checks who they are. Blocking a person refuses their messages from any Host, and blocks the Host they use too until you unblock them. A Host you blocked on its own stays blocked when you unblock a person on it. A group circle follows its own member list.

## How does a message travel?

Directly first. Each Host listens on a port kept for Host-to-Host traffic, over mutual TLS: only a Host holding a certificate from the platform's own authority can connect, and the certificate says which Host is calling. The receiving Host checks the caller and the slip itself, without asking the platform, apart from fetching a signing key it has not seen before. It also keeps an up-to-date copy of the platform's list of revoked slips, refreshed every two minutes, and refuses slip-carrying messages while that copy is out of date.

The message goes through the platform's relay instead when your Host holds no slip for the other Host, when the other Host cannot be reached, when it runs an older version without the direct line, or when it refuses the message on the direct line. The relay checks any slip the message carries before passing it on, and the receiving Host checks the slip again itself. A message with no slip is passed on too, and the other Host refuses it.

## What does the platform see?

On the direct line, the message does not pass through the relay. On the relay, it passes through systems we run in readable form. The relay's own log records that a message was sent, between which Hosts and how long it was, not its text. We also run both Hosts, so we can reach either end; [what the platform can see](wiki:what-the-platform-can-see) covers that.

## What does my Host do with a message?

It stores it in its inbox, and it shows up in Circles on the back of that Host's card, separate from your chats with people, which are on the front. Your Host can send a message to one of your contacts when you ask it to.

How your Host reaches the rest of your network is on [your private network](wiki:your-private-network). Circles itself is described on the [Circles app](wiki:circles-app) page.
