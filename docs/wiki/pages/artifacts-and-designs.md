---
title: Artifacts and designs
summary: Artifacts are live HTML pages your Host builds while you talk, which you can tune by pointing at them and roll back to any earlier revision. Your desktop is an artifact too, and the Designs app is a shelf of the desktops made on your Host, which you can put on with one click and take off with another.
section: building
order: 60
questions:
  - What is an artifact?
  - What is a visual?
  - Can I undo a change my Host made to an artifact?
  - Can my Host redesign my desktop?
  - Does my desktop work on my phone?
  - What is the Designs app?
  - How do I go back to my old desktop?
---

An artifact is a live HTML page your Host builds in the conversation: a mock-up, a report, a small tool, a whole desktop. It refreshes in the Artifacts window as the Host works on it, every change is kept as a revision you can go back to, and a good one can be turned into an app. Your desktop itself is an artifact, and the Designs app is a shelf of desktops you can try on.

## What can I do with an artifact?

Open it in the [Artifacts app](wiki:artifacts-app) and watch it change as your Host writes it. In tune mode you point at the design instead of describing it: click an element and it arrives in your next message as an exact reference, or double-click some text and edit it in place. When the Host offers several variants, they appear as tabs and "Use this one" makes your pick the main version straight away, without another turn of the conversation.

Every artifact is shown live in the gallery, and each one runs in a sandboxed frame. A finished artifact can be converted into an app on your Host. When your Host makes or changes an artifact during a conversation, the chat in Circles and the Workbench shows it as a small live preview; click it to open it in the Artifacts window.

## What is a visual?

A visual is a small page your Host draws right in its reply when a picture of the answer says it better than words: an animation, a diagram, a chart. It runs in the same sealed frame as an artifact, so it cannot reach the internet. A visual is not saved anywhere unless you choose to: each one has Save as artifact, Download, Copy and Full size. An artifact is what you keep and work on; a visual is part of the conversation. Visuals run only in your own Host's replies, in Circles and the Workbench; one in a message from someone else is shown as its source code.

## Can I undo a change?

Yes. Every create, write and in-place edit is saved as a revision, and the History strip in the viewer lists them. Click one to see that older version, and Restore to bring it back. The restore is itself a new revision, so going back can always be undone too. Every revision keeps every file, images and fonts included, and every revision is kept; a file that did not change is stored once however many revisions hold it. The oldest revisions are removed only when your Host's disk is too full to take the next one.

## Can my Host design my desktop?

Yes. The desktop is an artifact like any other: one HTML page that the shell shows under your windows, updating live as the Host works on it. The colours and fonts it declares carry into the shell and every app. Ask for a new look and the Host builds it in the conversation while you watch; "Use as desktop" in the Artifacts window puts the open artifact on your screen.

A desktop must meet a fixed standard before it can be used: it says whether it draws your apps itself or leaves that to the shell, declares a complete colour palette that is readable, shows only apps that are really installed, and loads nothing from outside. A design that falls short is refused with a report of exactly what is missing, so the Host can fix it. Before it goes on your screen it is also opened once in a real browser, the way the shell shows it: a desktop that stops with an error, crashes, leaves the page or keeps reloading itself, shows nothing at all, freezes, or leaves out the apps or the bar it says it draws (or takes them away again in its first seconds) is refused, with the error and the line it happened on. The same check runs every time your Host changes the desktop you are wearing. Your screen moves to a change that passes. A change that fails never reaches it: the screen stays on the last version that worked, and you and your Host are told why. If no browser can be started, the change is shown with a notice that it was not checked. While your desktop is open it is checked again later, each wait twice as long as the one before (up to six hours), and at once when you wear it again; if the browser is too old to run the check, it is not asked again until the browser is updated. A version that fails then is taken off again. A version that failed once stays refused until a browser passes it. A desktop that gets past the check and then reloads itself again and again on your screen, even slowly, is taken off by the shell and the version before it comes back, with a notice saying why. Only the shell saw that loop, so the version is not marked as failed: wearing it again checks it again, and if no browser can be started it goes back on with a note saying so.

## Does my desktop work on my phone?

Yes. When your Host puts a new desktop on, your phone wears it too. Your Host gives each desktop it designs a phone layout as well: your apps in a grid that fits the screen and scrolls, the clock and greeting at the top, and a different arrangement when you turn the phone on its side. The browser check opens every design a second time at phone size. One that breaks there is refused like one that breaks on a computer, and one whose apps run off the side of a phone screen comes back to your Host to fix. If you would rather have a different picture on your phone, choose it in Settings → Appearance under the phone wallpaper; it stays until the next desktop is put on.

## What is the Designs app?

The [Designs app](wiki:designs-app) is a shelf of complete desktops, each shown live. Yours holds every desktop made on this Host. Ours holds the designs that come with every Host; the shared library of published designs cannot be reached from a C-Series Host in this release, on its own or on the Privatae network.

"Wear it" puts one of your desktops on your screen at once. "Go back" restores the desktop you had before, and what your phone showed before.

## Can I share a design?

Not in this release. Sharing a design publishes it to the shared library, which a C-Series Host cannot reach. Every desktop made on your Host is already on your own shelf, Yours.
