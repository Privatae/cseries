---
title: Joining the Privatae network
summary: A Host works with no Privatae account, App Store included. With a membership code it can join the Privatae network, which adds the community board and remote access; membership is not on sale yet. The Host connects out to the network, and nothing the network sends can run on it.
section: network
order: 5
questions:
  - Do I need a Privatae account to use a Host?
  - What does joining the Privatae network give me?
  - How do I join or leave the network?
  - Can the network run anything on my Host?
  - What does my Host send to the network?
---

A Host works on its own, with no Privatae account, and installs apps from the App Store without one. Joining the Privatae network is optional and adds the community board and remote access, and membership is not on sale yet. The connection is made by your Host, outbound only, and nothing the network sends can run on it.

## What does membership give me?

The [Community](wiki:community-app) board, and remote access: once you turn it on, your Host can be opened from a phone or another computer anywhere, through the network, while it keeps running on your machine. Keeping your Host's encrypted backups in the network's storage is built into your Host, but the network does not keep backups yet. Until it does, your Host keeps them in a folder in its own storage, or in your own S3-compatible bucket if you choose one in Settings under Backup. Once the network keeps them, they go to the network's storage, unless you saved the folder or a bucket in that tab. The [App Store](wiki:app-store-app) does not need membership: every Host can read its free public catalog, and a member Host uses the network's store while the network answers. Everything else a Host does, including memory, models, keys, the bastion, coding and your own apps, works without membership and does not go through the network.

## How do I join?

Get a one-time code on your Privatae account page, at https://privatae.ai/account/network. The code works once and lasts 15 minutes. In the Settings app, open the Network tab, enter the code and choose Join. Your Host makes a key of its own, sends the network a certificate request, and keeps the signed certificate it gets back as its passport. Only the owner who is signed in can join or leave. Your Host cannot do either on its own.

If the network refuses your Host, for example because the code is not valid or the membership is not active, Settings shows the network's own words.

## What stays connected afterwards?

One outbound connection, kept open by your Host, verified against the network's own root certificate. Your Host sends it only a fixed list of requests: the App Store, the community board and its groups, renewing or revoking the passport, a heartbeat, the interests you list for introductions, turning on remote access and the tickets for its tunnels, and requests about its own backups, such as whether the network keeps them and how each backup ended. A request the network sends to your Host is refused as unsupported, so nothing it sends can run there. Your Host renews its passport by itself, with a fresh key each time.

Your Host's memory, conversations, private keys and files are not part of this connection, and neither are your model requests, which go from your Host to the provider you chose.

With remote access on, your Host also keeps a tunnel open, out to the network's relays. A browser's connection through it stays encrypted from the browser to a door on your machine, which holds the only key that opens it, and the browser still has to sign in to your Host.

## How do I leave?

In the same Settings tab, choose Leave the network. Your Host tells the network to revoke its passport, deletes the passport files, and goes back to running on its own.
