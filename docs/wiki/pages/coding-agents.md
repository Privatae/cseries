---
title: Coding agents
summary: Your Host builds code with coding engines you connect, such as Claude Code, Codex or Cursor. Each one is the vendor's own program, running in a container of its own, on your own account or key. You can run several on the same task and pick the result you like.
section: building
order: 20
questions:
  - Which coding agents can my Host use?
  - Can I use my own Claude Code or Codex subscription?
  - Where does the coding agent actually run?
  - Where is my coding agent login kept?
  - Can I run the same task on several coding agents and compare?
  - What happens if my default coding agent is not connected?
---

Your Host writes code by handing the work to a coding engine you connected: Claude Code, Codex, Cursor, Grok, Kimi Code, Antigravity CLI or opencode. Each is that tool's own program, running in a container beside your Host, on your own account or key. Privatae does not bill for the tokens. On a Host installed with `npx cseries`, the local runtime in the Host's Linux system starts those containers.

## Which coding engines are there?

Seven: Claude Code, Codex, Cursor, Grok, Kimi Code and Antigravity CLI each run on their own vendor's account or key, and opencode runs on a model-provider key of yours. You connect them in the Orchestrator app, where you can also set each engine's default model and effort and tick one engine as your default. A build that names no engine runs on the default.

## Where does the coding engine run?

Not in your Host. Each engine has its own container beside your Host, which is started when a build needs it and stopped when the build releases it or it sits idle. The container's working files go with it when it stops. Your Host holds the conversation, the plan and the memory; the engine does the coding.

A build has no time limit. By default an engine is stopped only when it shows no sign of life: no output, no processor use and no data moving for ten minutes, or one call to its model with nothing coming back for an hour. A quiet test suite or a long download is work, and is never stopped. Whatever the engine had written is kept.

## Whose account pays for it?

Yours. Some engines sign in the vendor's own way: Codex, Grok, Antigravity CLI and Claude Code run the vendor's own login program inside that engine's container, exactly as it would run on your laptop, and you finish it in your browser on the vendor's page. Where a vendor asks you to copy a one-time code back, your Host passes it straight to that waiting login.

The others use an API key you keep in the [Keys app](wiki:keys-app). Either way, it is stored on your Host so the engine stays connected between builds. What the engine uses is billed to your own account with that vendor, not by Privatae. See [Models and keys](wiki:models-and-keys).

## What if the engine I asked for is not connected?

The build is refused before any container starts, and the refusal names the engines that are connected. Your Host never swaps in a different engine. A build is also refused before any container starts when the project holds a link that points outside the project, and the refusal names each such link so you can remove it or replace it with a copy. If you have several engines connected and none ticked as default, the build asks which one rather than guessing.

## Can I run several engines on the same task?

Yes. A compare runs one task on several engines or models at once, each in its own copy of the project. You watch them work side by side on the project's page in the [Workbench](wiki:workbench-app) and choose what to keep: a whole run, or only some of its files, such as the design from one and the logic from another. Keeping a run copies its files into the project without rebuilding anything, leaves files the run did not touch alone, and can be undone.

## Can I save a way of working with several engines?

Yes. A multi-agent coding protocol is a small file you own: which engines run, one after another or at the same time, with which model and effort, and what to do on pass or fail. Your Host reads it and follows it. A protocol can be exported and shared as it is. It names only the kind of sign-in each engine needs, and one that contains anything that looks like a key or token is refused.

## Where do I see what an engine did?

Every run is numbered on the [Workbench](wiki:workbench) bench with its engine, model, result and time, and each Code Approval names the engine and model each change was built with.
