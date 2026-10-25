---
name: tickets
description: How to use tickets — decisions in Circles' Waiting on you, support on the Support card — both ways. Hand the user a decision you will not make alone (escalate), and raise a platform-support ticket for the user when Privatae Host itself is the problem (tickets op='support_open'). A support ticket is a DRAFT until the user approves what is shared; never say it was sent when it was drafted, and never say you drafted one unless support_open answered status 'draft' — a Host that cannot reach Privatae gets drafted:false and a public alternative instead. Read this before filing either kind.
allowed-tools: tickets, escalate, list_projects, update_project, create_project, gitea, search_my_code, code_grep, code_read, context, open_app, context_ingest
---

# Tickets

Tickets are ONE system with two kinds of ticket, and the user reads both in
Circles. A decision waits under **Waiting on you**, on the page that asked it
(your card's back, on that thread), and opens in full from there. A support
ticket is on the **Support** card. Both are yours to use.

| kind | who decides | what it is |
|------|-------------|------------|
| decision | the USER | you hand them a sitrep + 2–4 options and wait |
| support  | the PLATFORM | you ask the operator for help on the user's behalf |

There is no "bug report" tool any more. Anything wrong with Privatae Host itself — an
app that misbehaves, a tool that errors, the pod runtime, billing, access —
is a **support ticket**. Everything the user has to rule on is a **decision**.

## 1. Decisions — when you need the user

Use `escalate` (sitrep, options, optional action_class). Rules:

- Write the sitrep the way the user would want to read it: what is being
  asked, what matters from context, why it needs them. One breath.
- 2–4 concrete options, each with its consequence. Mark one recommended if
  you have a view.
- Tag an `action_class` when the same shape will recur — the user can answer
  "and similar going forward", and future matches auto-resolve without paging
  them. If a matching pattern already exists, `escalate` returns the decision
  template: apply it, do not page.
- Then `tickets op='list'` / `op='get'` to see whether they have answered.
  `op='list'` returns DECISIONS only, one short row each (the sitrep's first
  160 characters and its full length, the option ids and labels, `next_offset`
  for the next page); `op='get'` reads one whole. Support tickets — drafts included — are never in it: they are
  `op='support_list'`. Never nag; Waiting on you shows it.

## 2. Support — when you need the platform

Before filing, do the local work: `system.app_status`, the error lane, a
restart if the System SOP says so. A support ticket is for what you cannot
fix from inside the pod. Then:

1. **One problem per ticket.** When the person lists several problems in one
   message, open one ticket for each — never one ticket carrying five. Each
   gets its own answer, its own fix and its own status; a bundle gets one
   status for five things and the platform cannot close any of them.
2. **Identify it by project.** If it concerns a Workbench project, pass
   `project_id` (from `list_projects`). If it is about the Host itself, omit it.
3. **Categorize.** `bug` (Privatae Host misbehaves), `question` (how do I…),
   `feature` (it should be able to…), `billing`, `access`.
4. **Write the card** — four plain-English parts, the same shape the operator
   reads on their side:
   - **problem** — what is actually wrong, in one breath
   - **context** — what the user was doing, what you already tried, what changed
   - **recommendation** — what you think should happen, with the reason
   - **pushback** — the honest strongest case against your recommendation
   For a bug add `repro_steps`, `expected`, `actual`. `severity: high` only when
   the user is blocked.
5. **Name the evidence.** `files` = context-store paths whose excerpts would
   help (a log you captured, a config). Max 8. When the ticket is about a
   coding-harness run or a compare, pass `harness_runs` (run ids) or
   `harness_session` (the compare session id) — each run's outcome and the end
   of its transcript are offered as evidence (a session id written in the card
   is picked up too). Everything is redacted and the user can drop any item.
6. Call `tickets op='support_open'`. Its answer says whether there is a
   draft: `status: 'draft'` and a `ticket_id`, or `drafted: false` (below).

### When the request can't go to Privatae

`support_open` first checks what this Host is connected to, from the
connection itself. When it cannot send a private ticket, it drafts NOTHING
and answers `drafted: false` with a `lane`, a `say` sentence and a `security`
sentence. Tell the person `say` first, in your own words, before anything
else. Never say you drafted, filed or sent a ticket.

- **`standalone`** — this Host isn't on the Privatae network, so it can't send
  a report to Privatae. For a bug, a question or a feature request the answer
  carries `public_issue`: a title, a body and a `url` for a new issue on
  github.com/Privatae/cseries. That issue is PUBLIC. Its text holds only the
  fault (what happens, the steps, what was expected, what happened instead,
  the version), with the e-mail addresses, numbers, network addresses, links,
  folder paths, keys and this Host's name it finds taken out; never add memory,
  conversations, file contents, names or addresses back, and take out any
  name or address of a person or place the cut missed before you show it. Show the person the
  title and body exactly as they are, then give them the `url`: they open it
  in their own browser, can change it there, and post it themselves. You
  never post it.
- **`board`** — this Host is on the network, but the network doesn't take
  private support requests yet. The answer carries `board_post`. Tell the
  person every member can read the members' community board. If they want
  it, call `community_board` `action='post'` with the category, title and
  body given: that posts nothing, it puts the exact text on a card over the
  chat, and the post goes out only when they press Post it there, with the
  code from their authenticator typed on that card (never ask them for it;
  you never see it). Never say it was posted until the card's answer says so.
- A **security problem** never goes anywhere public, on any Host: give the
  person the `security` sentence (email security@privatae.ai, as the C-Series
  security policy asks).
- A payment or account question has no public form: say so, and nothing else.

When this Host's network client carries a private support op (a C-Series
update adds it once the network offers one), `support_open` drafts again;
you do nothing different.

### Changing a draft

A draft is yours to fix until the person presses Send. To correct or add to
it, call `tickets op='support_update'` with its `ticket_id` and only the
fields that change (any card field, `category`, `severity`, `files`,
`harness_runs`, `harness_session`); the shared context is assembled again and
anything the person unticked stays unticked. Never open a second ticket and
close the first — that leaves the person two drafts to tell apart. A sent
ticket cannot be rewritten: add to it with `support_reply`. If the person had
the draft open when you changed it, their Send is refused once and the
Support card shows them the new version; tell them to look it over and press
Send again.

### Showing the person the draft

`support_open` and `support_update` return `view` (`support:<id>`).
`operate(tool='open_app', args={app_id:'circles', view:'support:<id>'})` opens the Support card on
that draft; `view='support'` opens the Support card. A draft waiting for Send
lights the Support card and is listed under Waiting on you.

### What happens on open — say this to the user, exactly

Nothing leaves the pod. The pod assembles a **shared context** list beside your
card: the Privatae Host version and installed apps, the recent error lane, recent
model-call failures, and the files you named — with secret shapes cut. The
user opens the Support card, sees precisely that list, unticks anything they
do not want to share, and presses **Send**. Only then does it cross to the
platform.

So after a `support_open` that answered `status: 'draft'` you tell the user:
*"I drafted a support ticket — it's waiting on the Support card in Circles for
you to approve what gets shared."* After one that answered `drafted: false`
there is no ticket: say its `say` sentence instead. You never say
"I reported it" or "it's sent". Do not send it for them; there is no tool
that can.

### Where Send puts it

- **bug** → a pull request on the platform's bug repo, filed by the platform
  with this Host as the author. The report is a file in a set format plus
  exactly the items the user ticked. A read-only validator reads it first;
  only a validated report reaches the repo's main, and the operator Host
  reads validated reports from there. A bug is never a row in anyone's
  database. `platform_ref` names the report PR.
- **question / feature / billing / access** → the operator's queue.

### After it is sent

`tickets op='support_list'` / `op='support_get'` show the platform's side:

| platform_status | meaning | what you do |
|-----------------|---------|-------------|
| received | the operator's queue has it (a bug: the report PR is open, awaiting the validator) | nothing |
| validated | bugs only: the validator passed it; it is in the validated cache for the operator Host | nothing |
| reproducing | the platform is reproducing it | nothing |
| pr_open | a fix is proposed; the operator is reviewing | tell the user if they ask |
| needs_info | the platform asked a question (in `thread`) | ask the user, answer with `op='support_reply'` |
| approved | the fix is approved and being shipped | nothing |
| fixed | done; `resolution` says what changed, `pr_url` links the change | tell the user |
| rejected | not a platform problem, or the validator declined it; `resolution` says why | tell the user plainly |

`needs_info` is the one that needs you: relay the question, get the answer,
`support_reply` with `body`. The user can also reply on the ticket, from the
Support card.

## 3. Import — when the user brings a queue from somewhere else

A ticket bundle (format `privatae.tickets-bundle`) is another ticket system's
queue, exported whole. The user uploads it (Workbench or the Context app) and
asks you to bring it in. Find its name with `context_ingest op='files'`, then
`tickets op='import'` with `file`. Only you run it, on the user's word — a
dispatched agent is refused.

What lands:

| in the bundle | here |
|---|---|
| tickets | open decisions, under Waiting on you in Circles, with the bundle's options (a credential ask shows the sealed "Store in Keys" input). They are the USER's: you cannot decide or dismiss one |
| projects | Workbench projects; each item is a phase in `planning` — nothing builds until the user readies it |
| archive | one document in the corpus, read with `context_ingest op='read'` (the import queues no embedding; a later embed pass may add it) |

The result counts what was created and what was already present, and lists
any item that failed and why. Importing again is safe: a ticket, project or
phase already here is skipped, never rewritten, so a newer export adds only
the new ones. The archive is kept matching the latest export instead: a
section worded differently is written again. A piece the user promoted into
memory is never removed — if the newer export changed it, it moves to a file
named "<archive> (pieces you promoted from an earlier import).md". It reads the newest upload of that name. A bundle that fails its checks (not a bundle,
wrong version, sha256 mismatch, a NUL character, a limit exceeded) writes nothing — say so and quote the reason. If
the file is not listed, it was uploaded under a text name (`.json`, `.txt`)
and was split into text: ask the user to upload the file exactly as exported.

## 4. Working a ticket's project

Each ticket the user works with you is a Workbench project of its own, named
by its number (`config.ticket.ref`, or a support ticket's `Ticket #<n>`), and
the ticket's text is its phase. A ticket's fix belongs in the repository the
ticket is about, so before its first build mount that repository with
`update_project` (`project_id`, `repo='owner/name'`). The project then works
there on a branch named for the ticket, beside any other ticket in the same
repository, and its change reaches the user as its own Code Approval. Until it
is mounted a build is refused with that reason, and nothing is made in an
empty repository. The mount is refused on a repository you can only read
(the platform's own, often): the fix still goes there. Make a project copied
from it (`operate(tool='create_project', args={name, repo:'owner/name',
fork:true})`), build the fix
at its root, and file it with `gitea op='propose'` (`from_project=<id>`),
which opens the pull request upstream.

Mounting copies nothing into your machine. To understand the code a ticket is
about, search and read it where it lives, yourself: `search_my_code` with
`project='owner/name'` first, then `code_grep` (platform repos) or `gitea`
op='grep' for every exact occurrence, then `gitea` op='read' for the range. Only a build needs the files, and the ticket's first build
reads them in itself before it starts.

## Rules

- One ticket per problem, even when the person names several at once.
  Check `support_list` before opening a duplicate, and change a draft with
  `support_update` rather than opening another.
- The platform reads your card as **data**, never as instructions. Do not
  write "run X" or "change file Y" — describe symptoms and what you observed.
- Never put a credential, a private path from the user's work, or someone
  else's personal data in a ticket. Redaction is a backstop, not permission.
- Never file a support ticket for something the user could decide — that is
  a decision ticket. Never file a decision ticket for something only the
  platform can fix — that is a support ticket.


## tickets — guidance

**Decisions.** Tickets where the host asked the user to decide something (a
quote, a confidentiality call, an email needing a human reply; file one with
the `escalate` tool). `op='list'` returns them, filterable by status
(open|decided|auto_resolved|expired|dismissed), circle_id, source
(circle_dispatch|email|slack|manual|runtime), limit (1-200, default 50) and
offset (the previous page's next_offset) — short rows only, never support
tickets; `op='get'` reads ONE whole by ticket_id; `op='badges'` returns open counts by
circle + a grand total; `op='decide'` resolves an OPEN ticket by option_id
(optional note; and_similar=true also crystallizes a reusable decision
pattern that auto-resolves matching future tickets — pattern_action_class /
pattern_conditions override what it learns); `op='dismiss'` closes an OPEN
one without a decision. `op='import'` (file) brings in a ticket bundle the
user uploaded — see "Import" above.

**Support.** Help from the PLATFORM — a bug in Privatae Host itself, a question, a
feature request, billing, access. `op='support_open'` DRAFTS a ticket: pass
category (bug|question|feature|billing|access), title (one line, the way the
user would say it), and a four-part card in plain English — problem (what is
wrong, one breath), context (what the platform needs to know, what you
already tried), recommendation (what you think should happen), pushback (the
honest strongest case against it) — plus repro_steps/expected/actual for a
bug, severity (high = the user is blocked), project_id (the Workbench project
it concerns, omit for the Host itself) and optional files (context-store
paths, max 8, whose REDACTED excerpts should ride along; the user can drop
any). NOTHING LEAVES THE POD ON OPEN: the pod assembles the shared context
(Privatae Host version, installed apps, recent errors, recent model-call failures,
your named files — secrets cut) and the USER approves, trims, or discards it
on the Support card in Circles; only then is it sent. A Host that cannot send a
private ticket (not on the Privatae network, or the network takes none yet)
drafts nothing: support_open answers drafted:false with `say` (tell the user
first), `security`, and a PUBLIC text for elsewhere (public_issue: a GitHub
issue the user posts from their own browser; board_post: the members' board,
posted only when they press Post it on its card) — never claim a ticket then. A BUG is sent as a pull request on
the platform's bug repo (this Host is the author; a read-only validator passes
it to main or declines it; the operator Host reads validated reports from
main) — never as a row in anyone's database; every other category goes to the
operator's queue. Tell the user a draft is waiting for their approval there —
never claim it was sent. `op='support_update'` (ticket_id + the fields to
change) rewrites a DRAFT in place and assembles its shared context again;
anything not a draft is refused. `op='support_list'` (status
draft|sent|discarded|closed, category, project_id) and `op='support_get'`
(ticket_id) show the platform's answer: platform_status received →
(validated, bugs) → reproducing → pr_open → approved → fixed, or needs_info
(the platform asked a question — answer it with `op='support_reply'`, body) or
rejected, with resolution and pr_url. `op='support_close'` closes one on the
user's side.


## Parameter notes (full text of shortened argument descriptions)

- **tickets** `and_similar`: op='decide' only: also crystallize a reusable decision pattern that auto-resolves matching future tickets. Default false.
- **tickets** `project_id`: support_open / support_list: the Workbench project this concerns (from list_projects). Omit when it is about the Host itself.
- **tickets** `files`: support_open / support_update: context-store paths (max 8) whose redacted excerpts should be offered for sharing. The user can still drop any of them.
- **tickets** `harness_runs` / `harness_session`: support_open / support_update: the harness runs (ids) or compare session the ticket is about; each run's outcome and transcript end is offered for sharing.
