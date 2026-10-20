---
title: Building apps
summary: An app you build is described by a manifest that says what it is, how it runs, what it may reach and what permissions it asks for. You can install it on your own Host with one click, and publish it to other members through the Publisher, where every submission is checked and then read by a person before it appears.
section: building
order: 50
questions:
  - How do I build an app for my Host?
  - What is an app manifest?
  - What permissions can an app ask for?
  - How do I install an app I built on my Host?
  - Can I publish my app for other people to use?
  - Is my app reviewed before others can install it?
  - Can I charge for an app I publish?
---

You build an app the same way you build anything else: by describing it to your Host in the [Workbench](wiki:workbench). What makes it an app is its manifest, a short file that says what the app is, how it runs, what it may reach and which permissions it asks for. You can install it on your own Host with one click, or publish it so other members can install it on theirs.

## What does an app's manifest say?

In plain words, a manifest declares:

- the app's name, description, icon and version, and the window it opens in;
- how it runs (see below), and how much memory it needs when it has a process of its own;
- the outside web addresses it may reach, if any;
- the permissions it asks for;
- a short description of the app for your Host, so the Host knows what it is and where to find it;
- its author's terms: whether others may publish a website from it, republish it, or publish their own version of it;
- for data-bearing apps, which tables hold its records and who owns each row;
- for a process app in a machine of its own, an MCP server it runs for the Host it is installed on, which installing the app connects to that Host.

Your Host checks the manifest when the app is built, and the store checks it again when it is published, and refuses any entry it does not recognise, by name. A manifest the Host writes for a project is validated as it is written and rolled back if it is invalid, so a broken one is never kept.

## What kinds of app can I build?

An app you build is one of three kinds:

- **Static**: files your Host serves in a window, with no process of its own and no memory cost while it is closed.
- **Helper**: a small fetch-and-cache service in a container of its own, allowed to reach only the addresses the manifest lists.
- **Process**: your own server code, running in a container of its own built from a toolchain image. It can run only while its window is open, or stay running.

## What permissions can an app ask for?

Permissions come from a fixed list the Host defines, such as reading projects or publishing sites. An app gets only the permissions its manifest declares; a request for anything else is refused. Before you install an app from the App Store, it shows you the permissions it asks for, the outside addresses it may reach, where it runs and what memory it will draw.

## How do I install my app on my Host?

Choose Install as my app on the finished project in the Workbench, or ask your Host. The project is packed into a bundle signed by your Host, checked, and installed. An app with a container of its own (a helper or a process) needs the local container runtime, which is not finished in this pre-release, so it cannot start on a standalone Host yet.

## How do I publish an app to other members?

Publishing goes through the [Privatae network](wiki:the-privatae-network), so you need a network membership. Ask your Host to publish the project, or use the [Publisher app](wiki:publisher-app), where you also manage what you have published. You accept the publishing agreement, and the store runs its automatic checks. For the App Store, an app may only ask for permissions from a smaller list suited to apps that run on other people's Hosts; a helper or process app must state the memory it needs; a helper must list the addresses it reaches, and a process app must list any it reaches, including the package index it installs its pinned dependencies from; a process app must pin every dependency it installs; and the bundle must be one your Host packed from a committed project, so a hand-assembled zip is refused.

Every submission that passes is then read by a person before it appears. Only after that approval is the reviewed bundle signed, so what was reviewed is exactly what members install. You can publish to every member, or only to the members of a group you run.

## Can I charge for my app?

No. The App Store is free: a price above zero is refused, both when you publish and afterwards.

## Can someone copy my app?

Your terms travel with your app inside the signed bundle and cannot be edited after review. By default nobody else may publish a website from it or republish it as their own. Under the default terms, someone who publishes their own version of your app has to publish it as a declared flavour of yours, and the link back to your app is recorded. These terms decide what the honest path allows; they are not copy protection, and people review what is published.
