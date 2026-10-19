---
name: bastion-agent
description: How to BUILD a bastion agent when the user asks for one ("map my C drive and plan a cleanup", "index my contracts folder") — you author the job and ask for consent; the user's own machine does the work; you plan from the map it produces. Read this before proposing a store for that purpose; never promise to delete or move a file.
allowed-tools: dcn, get_network
---

# Building a bastion agent

A **bastion agent** is a small program that the user's bastion runs on the
user's own machine for ONE folder. It indexes that folder there, redacts
personal data before anything leaves, and answers your searches with
redacted text. The user's files never move. You do not write code to
build one — you write the **job** and ask for **consent**, and the machine
does the rest.

"Build a bastion agent to map my C drive and plan a data cleanup based on x, y, z"
is four steps. Three already exist as tools; one is a document you write.

## 1. Write the definition — `agent.md`

Before touching a tool, state the job in the user's words so they can
approve exactly that and nothing more:

```
# <name>            e.g. c-drive-cleanup
Purpose: <one sentence in the user's words>
Folder: <root>      e.g. C:\
Criteria: x, y, z   (what "worth keeping / archiving / removing" means to THEM)
Done means: <the artifact — e.g. "a ranked cleanup plan with sizes and reasons">
Not allowed: deleting, moving or changing any file — this agent only reads.
```

Keep it short. It is the contract between you and the user; it goes in the
bastion agent project alongside the manifest and, later, the plan.

## 2. Propose the store — the user approves with their code

```
dcn op=propose_store class=machine server=<bastion hostname> root=<folder>
    mask=[read, search, index] sweep=daily display_name="<name>"
    note="<Purpose line from agent.md>"
```

The draft is INERT until the user enters their step-up code in Context
Manager. **That approval is the install**: the bastion (0.13.2+) installs
and runs the bastion agent from the grant; from 0.13.3 it lives in
`<root>/.agent/`. It signs the job, indexes the folder, and serves it. You cannot approve it and cannot produce their code — tell them
it is waiting, and why: "approving lets your machine index <folder>
locally; nothing is copied off it."

`mask` is the ceiling, and two of its words do more than they say:
`search` is what makes the bastion run a bastion agent for the store at all
(no `search`, no agent — the folder is just listable); `index` is what
lets that agent build the catalog and vectors, so `catalog` needs it.
`read` lets you see the map and read redacted text. For a mapping job the
default `[read, search, index]` is exactly right; do not add `write`.
`note` carries the Purpose line so the approval screen says why.

## 3. Wait for the machine — read the card, not the clock

`dcn op=stores` → the store card. `freeagent: true` means serving — go to
step 4. Otherwise `freeagent_state` is the bastion's raw report,
`{state, serving, version, detail}`, and you translate it for the user:

| `state` | say |
|---|---|
| `missing-python` | "your machine needs Python 3.8 or newer (Microsoft Store on Windows); the bastion retries every 10 minutes, nothing else to do" |
| `missing-openssl` | "your machine needs openssl on its PATH (Git for Windows provides it); the bastion retries every 10 minutes" |
| `installing` | "it's fetching and signing the agent from your grant" |
| `running` with `serving: false` | the serve process is not up yet (binding, or restarting — `detail` says which); relay `detail` |
| `failed` | relay `detail` verbatim — that is the reason |

`serving: true` means the endpoint is up with a valid job — not that the
first index run has finished. The first-sweep signal is the catalog's own
state: `dcn op=catalog` answers `offline` with "no catalog yet" until the
first sweep completes. (`catalog` needs bastion 0.13.4; `search` 0.13.2.)

If `freeagent_state` is **absent**, do not guess the cause — there are
three, and they look the same on the card: the machine's bastion has not
checked in for 10 minutes, it is current but has not reconciled this
store yet, or it is older than 0.13.2. Tell them apart with
`get_network` (its bastions carry `online` and `bastion_version`; the
context_backend bastion list does not — it hides offline machines):
`online` false → the machine is off or disconnected; `bastion_version`
below 0.13.2 → they update it from the Network app's Bastions lens
(0.13.4 for catalog); otherwise wait — the bastion retries every 10
minutes. Never poll in a loop; tell the user what the card says and come
back when they do.

## 4. Plan from the map — then, and only then, read

`dcn op=catalog store_id=<id>` returns the **directory cards**: per folder,
file count, bytes, newest change, type mix, keywords, entity counts. That
is the map. Plan from it FIRST — sizes and ages rank a cleanup; type mix
finds media dumps and installers; keywords and entities find what is
personal. `kind=files` gives file cards (title, head, keywords, entities)
when a folder needs a closer look. `dcn op=search` answers a question over
the folder in redacted text; `read_range` with a hit's `doc` reads more of
one document. `[PII_*]` placeholders are redacted personal data — keep them
as they are and never guess what they hide.

Write the plan as `plan.md`: ranked, with the folder, the bytes, the
reason against the criteria, and the action the USER would take. Cite
`store + directory` — the map is the citation. Where it lives: the
bastion agent project (one folder per agent: `agent.md`, `manifest.yaml`,
`plan.md`) once that project type exists; until then keep `agent.md` and
`plan.md` with the conversation and say so. You cannot write into
`<root>/.agent/` — that is the machine's, not yours.

## 5. Reorganize — a signed move plan the user approves (archive, never delete)

When the user wants the folder actually reorganized, the plan becomes a
**move plan**: an ordered list of `{from, to}` inside the granted root,
proposed by you, approved by the user with their code, executed by their
machine. "Remove" means **move into the archive folder** (`archive`,
default `Archive/` under the root). Nothing is ever deleted — the user
empties the archive by hand when they are sure.

```
dcn op=propose_plan store_id=<id> title="Downloads cleanup" archive="Archive"
    moves=[{from:"Downloads/setup-2023.exe", to:"Archive/installers/setup-2023.exe", size_bytes: 8123456},
           {from:"Downloads/IMG_2041.jpg",   to:"Pictures/2024/IMG_2041.jpg"}]
```

Rules that hold on both ends (the machine re-checks every one):
- paths are relative to the root, slash-separated, no `..`; `.agent/` is
  never part of a plan;
- a target that already exists is a refusal, never an overwrite; a source
  that is gone is a refusal;
- one plan, at most 5000 moves — split a big cleanup by folder;
- include `size_bytes` from the file card when you have it: the verify pass
  checks it after the move.

Then: the user approves the plan in Context Manager (their code — you
cannot). If they ran `privatae-bastion stepup-enroll` on that machine, the
approval happens ON the machine instead: Context Manager says "waiting for
your approval on <machine>", and there they run `privatae-bastion approvals`
(the moves, in plain words, from details the machine verified itself) and
`privatae-bastion approve <id>` with a code from that authenticator. The
approval is bound to exactly these moves; the machine will not apply a plan
it did not see approved, and you never see the code. You run the plan
yourself: `operate(tool='dcn', args={op:'run_plan', plan_id, mode:'dry_run'})`
checks every move on
the machine and returns `ready` or `problems` (fix the plan, re-propose).
`mode:'apply'` runs the same check (it holds your turn while the machine
works; say so before you start it), renames in order, rolls back on the first
failure, verifies every target and source, and returns the report (also
kept on the machine at `<root>/.agent/plans/<id>.json`). Report it as it
is: `applied`, `verified`, and any `mismatches`. An applied plan cannot be
run twice; further changes are a new plan.

## What you must not promise

There is no delete on this surface, and that is by design: an agent given
a whole drive reads and, when the user signs a plan, moves — it never
removes. If they ask you to "just delete it", say plainly that the plan
moves it to the archive and they delete from there by hand. A plan is the
only write: no rename-in-place edits, no content changes.

One agent per folder, one machine per folder. If the user wants a second
folder mapped, that is a second store and a second `agent.md`.
