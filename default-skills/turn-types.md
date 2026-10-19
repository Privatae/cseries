---
name: turn-types
description: How to set up a turn type for the user — a profile (tools, keys, servers, skill, context, model) that a kind of turn runs under — with the turn_type tool, and why only the user's click makes one live, changes it or retires it.
allowed-tools: turn_type, escalate, context
---

# Turn types

A turn type is the user's area of work, as a profile: the tool groups, keys,
servers, skill, context and model that turns of that kind run with. Inside a
type, everything not listed is refused at the tool call. A thread with no
type is the plain chat, exactly as always.

The platform decides which type a message is before the turn, from each
type's keywords. You never classify a message yourself.

## Setting one up

When the user asks for one ("set up finance for me"):

1. Build around it with the tools you already have: a project or bucket,
   apps, scheduled tasks, and a skill of your own for the type's steps
   (write it yourself: `operate(tool='context', args={action:'write',
   path:'skills/<name>', content:…})`; the storage skill has the standard).
2. Draft the type: `turn_type op='draft'` with `name` and `spec`. Keep it
   small. List only what that work needs:
   - `keywords`: words that mean this work, the way the user talks about it.
   - `tool_groups`: the groups the work calls (your map names them).
   - `grant`: capability names, the same ones a dispatch grant uses.
   - `keys`: credential names it may use; `autofill:<site>` for each site
     it signs in on, a browser agent's site and its allow hosts included;
     `mcp:<server>` for each connected MCP server whose tools it calls.
   - `bastions`: servers it may request ops on, by hostname; `*` only if it
     must reach a server it cannot name, such as enrolling a new one.
   - `skill`, `context` (`projects`, `documents`, `memory`: all, projects
     or none) and `model`, when the work needs them.
3. One card goes up. The platform writes it from your draft and says what
   the type can and cannot do. End your turn. The user answers it.

A draft does nothing. You cannot activate, widen or retire a type. There is
no op for it.

## Changing or retiring one

`op='change'` with `type_id` and the fields that change asks the user to
change an active type. When the change adds something, the card also offers
Once: the next turn of that type gets it and the type stays as it is.

`op='retire'` asks to retire one. Retiring means its thread runs with
everything you hold, so it is the user's click too.

One card per type at a time. A draft the user said Not now to is still
yours: draft the same name again with a new spec, or discard it with
`op='retire'` (discarding a draft needs no card).

## When a typed turn is refused

The refusal names the types that hold what you reached for. Say in the
thread what you could not do and which type holds it; the user takes it
there. If this type should hold it, ask with `op='change'`.

You cannot schedule work for another type. Nothing passes between turns
except what the thread shows: the thread is your chain of thought. The
Timeline at the top of each turn shows what else is running, finished or
due; for another type's work it shows the type, kind, id and time, never
the work's own words.

## The root thread

One type is built in, not drafted: Root. Its thread is where `machine_exec`
runs, and nothing untrusted reaches it. `operate(tool='turn_type', args={op:'root', request:…})`
opens it, or continues it, with your request as a turn of its own that the
user sees in the thread list. Nobody widens it: its tools and context are
fixed by the platform.
