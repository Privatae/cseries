---
title: Publishing a website
summary: A Host does not serve websites. Its Publish as site step hands a project to a public web service that is not part of the Host, and neither a Host on its own nor the Privatae network can reach that service in this release, so publishing a site is not available.
section: building
order: 40
questions:
  - How do I publish a website from my Host?
  - Can I use my own domain for a site?
  - Does my Host serve websites itself?
  - Do I upload files to publish a site?
---

Your Host can build a website in the Workbench, but it does not serve one. The Publish as site step packs the site and hands it to a public web service, and that service is the one that would serve it. A Host on its own has no connection to that service, and the Privatae network does not carry it to a member Host, so in this release the step cannot complete. The same goes for your own domain, which is set on that service.

## Does my Host serve websites?

No. The Host's dashboard answers only on your own machine, on 127.0.0.1. A site you want other people to open has to be served somewhere else. You can still build it in the [Workbench](wiki:workbench) and take the built files from the project.

## What does the publish step pack?

The built folder (dist, build, out or public) when it has a page, otherwise the project's own index.html. A static site never includes hidden files such as .env, and a project built with a bundler such as Vite must be built first; its unbuilt page is refused with a message saying so. There is no upload step and no zip to prepare.

The [Sites](wiki:sites-app) app is the control panel for that service, so it works only where the publish step does.
