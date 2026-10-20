---
title: What a Host is
summary: A Host is one AI that runs on your own machine, in a Linux virtual machine, as two containers, the Host and its PostgreSQL database. Its memory, apps and files live on volumes that survive an update, and its dashboard answers only on your own machine.
section: host
order: 10
questions:
  - What is a Host?
  - Is a Host just a chatbot with a new name?
  - What is inside a Host?
  - Does my Host have a public IP address or open ports?
  - Where does the AI model actually run?
  - Can Privatae get into my Host?
---

A Host is one AI that belongs to you and runs on your own machine. It has its own PostgreSQL database, its own apps and files, and its own rules. It is where your AI remembers you and does its work. The installer puts it in a small Linux virtual machine, so it is sandboxed from the rest of your machine, and the dashboard you use it through opens in your browser.

## Is a Host just a chatbot?

No. You talk to it, but it is a program with the properties a program has: a place it runs, disks it keeps, and doors that are closed by default. The model that writes its replies is not inside it (see below). Change models and the Host, its memory and its rules stay where they are.

## What is inside a Host?

Two containers run together:

- **The Host itself.** The program that thinks, works and serves your dashboard. Its root filesystem is read-only, it runs as an ordinary user, and every Linux capability is dropped.
- **Its database.** PostgreSQL, in a container of its own. It listens on a Unix socket shared with the Host and on no network port.

Beside them are volumes that survive when the containers are replaced. One holds the database: every conversation and everything the Host remembers. Others hold what the Host installs and makes: your apps, your projects, your files and its secrets. When a new version is installed, the containers are recreated and the same volumes are attached again. [Updates and versions](wiki:updates-and-versions) covers what changes and what stays.

## What does the network see of it?

The dashboard is published on 127.0.0.1 only, so only your own machine can reach it, and it serves plain HTTP. Two more ports are published the same way for the bastion on your machine: one where the bastion pairs and one where it connects in. Nothing is open to the public internet. To use the Host from another machine you put your own reverse proxy in front of it, one that ends TLS; the README says how.

## Where does the model run?

Outside the Host. For each of its roles the Host uses the provider and model you chose, on your own key, or a model server running on your machine. The Host is the resident; the model is a route it takes. [Models and keys](wiki:models-and-keys) has the details.

## Can Privatae get into my Host?

Privatae does not run your Host, and a Host on its own accepts nothing from Privatae: it has no platform connection to receive anything on. If you join the Privatae network, your Host makes one outbound connection to it and refuses any request the network sends it to run; [joining the Privatae network](wiki:the-privatae-network) says what that connection carries. [How your Host is protected](wiki:how-your-host-is-protected) covers the walls around it.
