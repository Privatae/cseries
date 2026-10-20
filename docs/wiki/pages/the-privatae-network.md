---
title: Joining the Privatae network
summary: A Host works with no Privatae account. With a membership code it can join the Privatae network, which gives it the App Store and the community board. The Host connects out to the network, and nothing the network sends can run on it.
section: network
order: 5
questions:
  - Do I need a Privatae account to use a Host?
  - What does joining the Privatae network give me?
  - How do I join or leave the network?
  - Can the network run anything on my Host?
  - What does my Host send to the network?
---

A Host works on its own, with no Privatae account. Joining the Privatae network is optional and adds the App Store and the community board. The connection is made by your Host, outbound only, and nothing the network sends can run on it.

## What does membership give me?

The [App Store](wiki:app-store-app), where you find and install apps and where you can publish your own, and the [Community](wiki:community-app) board. Everything else a Host does, including memory, models, keys, the bastion, coding and your own apps, works without membership and does not go through the network.

## How do I join?

Get a one-time code on your Privatae account page, at https://privatae.ai/account/network. The code works once and lasts 15 minutes. In the Settings app, open the Network tab, enter the code and choose Join. Your Host makes a key of its own, sends the network a certificate request, and keeps the signed certificate it gets back as its passport. Only the owner who is signed in can join or leave. Your Host cannot do either on its own.

If the network refuses your Host, for example because the code is not valid or the membership is not active, Settings shows the network's own words.

## What stays connected afterwards?

One outbound connection, kept open by your Host, verified against the network's own root certificate. The network answers only a fixed list of requests: the App Store, the community board, renewing or revoking the passport, and a heartbeat. A request the network sends to your Host is refused as unsupported, so nothing it sends can run there. Your Host renews its passport by itself, with a fresh key each time.

Your Host's memory, conversations, keys and files are not part of this connection, and neither are your model requests, which go from your Host to the provider you chose.

## How do I leave?

In the same Settings tab, choose Leave the network. Your Host tells the network to revoke its passport, deletes the passport files, and goes back to running on its own.
