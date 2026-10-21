---
title: Browser
summary: Browser is a real web browser that runs for your Host and appears live in the app window. It starts when you open it, shuts down when you stop using it, and can record a task you do once and repeat it on a schedule with no AI involved. You install it from the App Store.
section: apps
order: 10
app: browser
questions:
  - Does my Host have a web browser?
  - Does the browser use memory when I'm not using it?
  - Can I automate a website task without an AI model?
  - Will the browser remember my logins?
  - Why won't the Browser open?
  - How do I get the Browser app?
---

Browser is a full web browser that runs in a container for your Host, shown live in the app window. You type an address, click, scroll, type and sign in as you would in a browser on your desk, and the pages it opens appear as tabs above the picture. It is not tied to any site. It is not built into the Host: you install it from the [App Store](wiki:app-store-app), or ask your Host to install it.

## Does it use memory when I am not using it?

No. It runs in its own container beside your Host, starts when you open the app, and shuts down on its own once you stop using it. While it runs it takes up to 1 GiB of the machine's memory.

## Will it remember my sign-ins?

Not by itself. It keeps nothing between sessions, so whatever you were signed into ends when it shuts down. To keep sign-in details, use **Save these entries** on a filled-in form: you pick which fields to keep, passwords are saved without being shown, and they are sealed in [Keys](wiki:keys-app).

## Can it repeat a task for me?

Yes. Press **Create agent**, name it, and do the task once by hand: sign in, click through, download. When you stop, you review the steps, remove any that should not be repeated, and choose to run it only when asked or every hour, day, week or month. A saved agent is a real browser repeating your steps; no AI decides anything. If a site asks for a code only you have, the run pauses and asks you. Before an agent may sign in on its own, you allow that site under Sign-ins in Keys. Runs that fail or pause show in the app's Agents list and in your inbox. [Agents](wiki:agents) explains why most work needs no model.

## Can a website see my files?

Only if you grant it. **Grant to project** gives a site that asks for a local folder the files of one of your projects. With nothing granted, a site gets no access to your projects' files.
