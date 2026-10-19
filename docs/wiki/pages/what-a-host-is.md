---
title: What a Host is
summary: A Host is a machine of your own in the cloud. It is one pod with its own database, its own disks, its own certificate and its own place on your private network, and it does not depend on your laptop being open.
section: host
order: 10
questions:
  - What is a Privatae Host?
  - Is a Host just a chatbot with a new name?
  - What is inside a Host?
  - Does my Host have a public IP address or open ports?
  - Where does the AI model actually run?
  - Can Privatae get into my Host?
---

A Host is one machine that belongs to you, running in the cloud. It is a pod in a namespace that belongs to that Host alone, with its own database, its own disks, its own certificate and its own address on your private network. It is where your AI remembers you and does its work, and it does not depend on your laptop being open. A Host stays running when it sits idle; it is not put to sleep between requests.

## Is a Host just a chatbot?

No. You talk to it, but it is a machine, with the properties a machine has: a place it runs, disks it keeps, a certificate it answers to, and doors that are closed by default. The model that writes its replies is not inside it (see below). Change models and the Host, its memory and its rules stay where they are.

## What is inside a Host?

Three containers share the pod:

- **The Host itself.** The program that thinks, works and serves your desktop.
- **Its database.** PostgreSQL, in a container of its own. The Host reaches it over a socket inside the pod. The database container declares no network port, holds no certificate and has no secrets mounted.
- **A small network container.** It holds the Host's WireGuard key and ends every tunnel from your own machines.

Beneath them are two disks that survive when the pod is replaced. One holds the database: every conversation and everything the Host remembers. The other holds what the Host installs and makes: your apps, your projects, your files. When a new version rolls out, the containers are recreated and the same disks are attached again. [Updates and versions](wiki:updates-and-versions) covers what changes and what stays.

Some work runs beside the pod rather than in it. The browser behind the Browser app, and the coding engines that build your projects, each run in a sandbox of their own in your namespace. They start when they are needed and are stopped after a spell of sitting idle, so code a coding engine writes never runs inside the Host.

## What addresses does a Host have?

Two, facing different ways. On the cluster it has a private address, which is not on the public internet. On your own network it has a second address that only the machines you have paired with this Host can reach. Each of those machines makes its own key when it pairs. [Your private network](wiki:your-private-network) and [the bastion](wiki:the-bastion) explain how a machine joins.

## What does the public internet see?

No address and no open port. You come in through the front door you sign in at, from any browser, with nothing to install. Your own machines come in through a private tunnel that each machine dials out itself.

## How does the Host reach the internet?

The Host's pod has no direct route out. Requests to model providers and websites leave through an exit proxy on the server the Host runs on, which we operate, so a request to a model provider always crosses systems we run. An embedding model running on one of your own machines is reached through your private tunnel instead. The outbound rules you can approve, and the option to send named traffic out through a machine of your own, are covered in [your private network](wiki:your-private-network).

## Where does the model run?

Outside the pod. For each of its roles the Host uses the provider and model you chose, on your own key or sign-in. The Host is the resident; the model is a route it takes. [Models and keys](wiki:models-and-keys) has the details.

## Can Privatae get into my Host?

Yes, and we say so plainly. We run the cluster your Host lives in, so we can reach into its pod and its database, and our staff and automated checks do run commands inside Hosts to operate and fix the service. The front door you sign in at is ours too.

Backups leave the pod too. Your Host backs itself up, encrypted on the Host only to keys you hold: the recovery key you make on your account page, and the key of any bastion you add. Without a recovery key it takes no backups of its own. Separately, once a day, at 02:00 UTC unless you change the schedule in Settings, a platform job copies your Host's database, apps, files and code repositories, encrypts them and stores them. We hold a key that opens that copy. [What the platform can see](wiki:what-the-platform-can-see) lists the rest, and [how your Host is protected](wiki:how-your-host-is-protected) covers the walls around it.
