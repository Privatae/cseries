---
title: App Store
summary: The App Store is where you find and add apps to your Host and see what each one asks for before you choose it. It works without any Privatae account, from a free public catalog that every Host can read.
section: apps
order: 10
app: app-store
questions:
  - How do I install an app on my Host?
  - Do I need a Privatae account to use the App Store?
  - How do I know how much memory an app will use?
  - What does an app get access to when I install it?
  - How do I update or remove an app?
  - Why can't I find Workbench or Circles in the App Store?
---

The App Store is where you find apps for your Host and add them, and see where each one runs and what it takes before you choose it. It is not one of the apps built into the Host. It needs no account and no membership. Each release publishes a signed public catalog of free apps, and any Host reads it: the catalog's signature, each app package's checksum and the package's own signature are all checked on your Host before anything installs. A Host that has joined the [Privatae network](wiki:the-privatae-network) uses the network's store while the network answers, and the public catalog whenever it does not.

## What is in the App Store?

Two shelves. **On your machine** holds apps that install on your Host. **For your public sites** holds ready-made web applications for sites; see the [Sites](wiki:sites-app) app. Tabs show the updates waiting and what you have installed, and one search box covers both shelves.

The apps built into every Host are not listed, since there is nothing to choose.

## How do I know how much memory an app will use?

Each card says where the app runs. Most apps are plain windows that use no memory in the background. An app with a process or container of its own says so, and, where it declares one, how much memory it takes while it runs. A strip at the top shows the machine's memory and what is using it now.

## What does an app get when I install it?

Before anything installs, the app's details list the outside addresses it may reach, the permissions it asks for, where it runs and how much memory it takes. The button reads **Install & allow access**, because installing grants what is listed there. Every app's package is checked against a trusted signature before it installs, and one that fails the check is refused. An app that runs an MCP server for your Host becomes one of your Host's connections when it installs, so your Host can use its tools.

## How do I update or remove an app?

The **Updates** tab shows any newer version waiting; an app moves to it when you choose to update. **Uninstall** on an installed app removes it; it asks you to click a second time first. Uninstalling also takes back what the app was given: its MCP server stops being one of your Host's connections, and an account you paired through a messaging app is cut off. Settings, Memory and Orchestrator are part of the Host itself and cannot be removed. [The apps on a Host](wiki:apps-overview) lists every app and where each one comes from.
