---
title: Publishing a website
summary: A project can be published as a public website, but the sites are served by a public web service that is not part of the Host. A Host on its own, and a Host that has joined the Privatae network, cannot reach that service in this release, so publishing a site is not available.
section: building
order: 40
questions:
  - How do I publish a website from my Host?
  - Can I use my own domain for a site?
  - Does my Host serve websites itself?
  - Do I upload files to publish a site?
---

Your Host can build a website in the Workbench, but it does not serve one. The Publish as site step hands a signed bundle to a public web service, and that service is the one that serves the site, holds its domain and takes payments. A Host on its own has no connection to that service, and the Privatae network offers a member Host only the App Store, the community board and its own passport requests, so in this release the step cannot complete.

## Does my Host serve websites?

No. The Host's dashboard answers only on your own machine, on 127.0.0.1. A site you want other people to open has to be served somewhere else. You can still build it in the [Workbench](wiki:workbench) and take the built files from the project.

## What does the publish step do?

On a finished Workbench project, Publish as site packs the built folder (dist, build, out or public) when it has a page, otherwise the project's own index.html, and hands it to the public web service. A static site never includes hidden files such as .env, and a project built with a bundler such as Vite must be built first; its unbuilt page is refused with a message saying so. There is no upload step and no zip to prepare.

## What does the public web service add?

The [Sites](wiki:sites-app) app is the control panel for it: who may visit a site, your own domain, payments into your own Stripe account, and a database per site. These belong to that service, so they work only where the publish step does.
