---
name: memory
description: The host's own memory surfaces beyond recall — the user's stated taste (preference), saved links, the readable conversation log (chat_threads), keeping, correcting or dropping a memory (memory_curate), the learned rules, and the two user-owned settings (personality weights, explanation level) that the host reads but never moves on its own.
allowed-tools: preference, links, chat_threads, memory_curate, training_memory, personality_state, explanation_level, project_adopt_thread, recall
---

# Memory SOP

## preference — guidance

Acceptance (review /pick and /merge) is captured automatically; this is for
what the user SAYS, which used to be lost the moment the conversation moved
on. `op='like'`/`'dislike'`: a BIAS toward or against a target (a model id, a
harness id such as 'codex', or a named design choice such as 'the timeline
layout') — record the user's own words in `text`, they get quoted back into
future build briefs. `op='ban'`: a RULE, not a bias — the target is REFUSED
at dispatch (every build, compare and agent) before any pod wakes or anything is
billed; always global, so use it only when the user means never ("never use
amazon nova"). A ban only refuses when subject is 'model', 'harness' or
'general' (the default); subject='design' records a look the user ruled out
and does NOT block a dispatch, so pick the subject by what they actually
meant. `op='unban'`: lift a ban. `op='tokens_get'`: the design tokens a coder
should be handed. `op='tokens_set'`: store design tokens ({name: value},
merged unless replace=true) — these are DELTAS on top of whatever design
system the Designs app assigned, for the values the user keeps restating.
Everything the user says is kept whole — every note, every stated reason,
every token at full length. A build brief names every like and dislike and
every token, and quotes the user's words highest-ranked first; when some do
not fit it says how many, and `op='list'` reads them all.
`op='prefer'` (target = a model, `work` = foreign_repo, web or message): the
model an agent you dispatch for that kind of untrusted work runs on when the
dispatch names no model; with none set it runs on yours. The user may set it
(`by='person'`, their words in `text`) and so may you (`by='host'`, default),
in the open: `op='list'` shows it under work_models with who set it.
`op='unprefer'` (work) removes it. A banned model cannot be preferred.
Call this whenever the user states a preference, a dislike, or a never — do
not wait to be asked to remember it.

## links — guidance

This is the answer to "what was that thing I found" — check here before
telling the user something is lost, and before searching the web for a page
they have already kept. It matters most alongside the Browser app: that
browser shuts down when nobody is looking at it and remembers nothing, so a
page only survives if it is saved here. When the user finds something there
and says keep it, save it. The note is the "why I kept this" line and it is
what search matches best, so write one. Use `get` to check whether something
is already saved before saving it again. There is no bulk remove and no
delete-by-pattern; do not offer one.

## chat_threads — guidance

When the user references a past discussion you can't see in context, go:
recall → episodes → read — never claim a conversation is lost without looking
here first. `read` with since/until (ISO, from an episode's started_at/
ended_at) loads exactly one past conversation.

## personality_state — guidance

Your personality settings are what the user set: ten slider weights that
never move on their own. They change only when the user moves the sliders in
Settings (Response Shaping) or asks you in words. What you learn from the user
lands in rules and your own tables, never in these weights, so never tell the
user you are "drifting warmer" or "learning a personality from them".
Call this to explain how you are currently tuned, or when the user asks
whether you have changed.

## explanation_level — guidance

Set it ONLY when the user asks in words ('less detail', 'just the command',
'explain more', 'spell it out'): that is the user moving their own setting.
Never set it from your own judgement of the user, never from a guess about
what they know, and never say you have "learned" their level — it is theirs
and it changes only when they change it. After a set, say the new position in
plain words. To show the user the setting itself, open Settings on Response
Shaping: `open_app` with app_id 'settings', view 'shaping'.

## project_adopt_thread — guidance

Use when a general-chat discussion became a project — the alignment
conversation belongs with the project it produced.

`kind='project', ref='<source project id>'` folds ANOTHER project's thread into
this one ("fold 228 into 229"): its turns and the memories derived from them
are retagged, a marker stays in the source, its phases and repo stay where
they are (archive the source afterwards with `archive_project` if it is now
empty). Use it to repair a conversation that got split across two projects.


## memory_curate — guidance

It does what you tell it and nothing else; no second model weighs in.
`op='add'` stores a preference (the default) or a relationship note.
`op='replace'` with the `memory_id` recall shows stores the new text where
the old row was read and retires the old row into it, so the old version
stays in the correction history and is never repeated as current. Use it when
the user corrects a memory. The correction belongs to this conversation:
deleting it brings the old row back. A preference said in several
conversations that nobody has scoped is scoped with `memory_scope` first. `op='drop'` retires a row with no replacement. The same words held
already are not stored twice. Facts, decisions and summaries are not stored:
the conversation itself is, and recall finds it. A constitutional rule and the
transcript are never retired here.

When recall missed something you then found another way, fix the next search:
`op='alias'` with the `memory_id` and the words you searched with adds them to
that memory, and recall matches them from then on. The memory itself is
unchanged. Facts with a shape of their own (a kitchen, a routine, a list) go in
your own database instead: skill `world`.

A rule (one the user taught with "Remember this:", or one you learned) is a
preference marked `[rule]` in `memory_scope`'s list. It rides in "Things You
Were Taught", and nothing automatic ever retires it. When the user asks you
to change or forget one, `replace` or `drop` it here, in the open; a
replacement stays a rule, read everywhere the old one was, including every
place the user taught it (the result names those places). "Things You Were Taught" can also hold a stored
skill that is not a `[rule]` (recall shows it as `skill_memory`): this tool
cannot change or drop it, and the user removes it in the Memory app. When one
should change or go, raise it with the user through `escalate`, naming the
entry and why; removing it is theirs. What you learn in a turn you can also
keep in `close_turn`, which scopes it by what it is about.

A Training Memory rule (the behavioural rules `training_memory` reads) is
read-only to you: nothing you call changes or retires one. When one is wrong,
stale, or contradicts what the user now wants, raise it with them through
`escalate`, naming the rule and why. Retiring it is the user's decision, never
yours.

A memory is what the person said, wanted, decided or asked, or who they are
(`about='person'`). The state of a system — a tool, harness, service, key,
build, project phase, an error or a version — is `about='machinery'`: it is
refused, because it is read live every turn and a stored copy is repeated as
current long after it stopped being true. Store what the person decided about
the system instead ("they want builds pinned to the known-good model"), never
the system's state ("claude-code fails without a pinned model").

The same rule holds for a `<learned>` rule: `about="practice"` is how to work
and is kept; `about="machinery"` is not stored.


## memory_scope — guidance

A preference rides only where it belongs. What was relevant in one place is a
leak somewhere else. Set its scope by what it is ABOUT, never by where it
happened to be said:

- `global`: it is about the person or how they like work done ("prefers
  metric units", "short answers first", "check the logs before retrying").
  It rides any turn it is relevant to.
- `project` / `app` / `thread` (with the id as `ref`): it is about that work's
  data or details ("keep credit cards out of general bills" belongs to the
  finance app). It rides only there.

Personal details that belong to an app (money, health, family) are never
global.

- `kind='list'` shows every preference with its `#id`, grouped by scope, with
  project and thread names; a group marked `[where said]` holds the ones
  nobody has scoped yet. `recall` also shows a preference's `#id`. A rule is
  marked `[rule]`; one nobody has scoped rides everywhere until you scope it.
  A rule you narrowed to a place still rides where the user taught it
  ("Remember this:", first time or again): the list marks it
  `[rule; also where taught: …]`.
  A preference marked `[held by rule #N]` says that rule's words: it is kept
  where the rule is not read (a project's typed turns) and never shows
  beside the rule in a turn.
- A preference nobody has scoped rides where it was said: its own
  conversation and every other one that said it again. That never leaks, so
  widening one to `global` is a decision you make, never a default. Tidying
  never moves one: two preferences are folded into one only when they ride in
  exactly the same places, and the survivor keeps the decision.
- A `project` ref is the project's id, a `thread` ref the thread's id, an
  `app` ref an app that has had turns (a wrong one is refused with the list).
- Every change is kept in the row's `scope_history`, so it can be undone. A
  scope you set stays when the preference is restated elsewhere or its
  conversation is moved into a project.
- A preference you write in `close_turn` is scoped the same way. A scope with
  no `ref` means this turn's own project, app or thread. Saying a preference
  you already hold never narrows it: if it already reaches that place (or
  everywhere), nothing changes; said as `global`, the held one widens; said
  for another place, that place gets its own, and the two are kept apart
  from then on. A rule's words are the exception: `close_turn` never widens a
  rule or keeps a copy of it elsewhere; it answers `held_by_rule` with the
  rule's `#id`, and `memory_scope` is how to change where a rule rides.
  To narrow one, use `memory_scope`. A rule you narrow still
  rides where the user taught it, and only there besides; the result names
  those places ("still read where the person taught it: …"). Saying it again
  yourself never adds one.

## Parameter notes (full text of shortened argument descriptions)

- **preference** `op`: list=read everything (default); like/dislike=bias; ban=refuse before a run (global); unban=lift; tokens_get/tokens_set=design tokens.
- **preference** `target`: What the preference is about — a model id, a harness id, or a named design choice. Required for like/dislike/ban/unban.
- **preference** `project_id`: Scope to this project. Omit for the standing cross-project preference. Ignored for op='ban' (bans are always global).
- **links** `action`: save=keep a url; list=recent (default); search=find by words; get=one by id/url; remove=delete one by id.
- **chat_threads** `op`: list=all threads; episodes=one thread's conversations; read=turns (optionally one episode via since/until).
- **training_memory** `role`: Optional role to scope to (e.g. 'coding', 'research', 'writing'). Omit to get the always-firing unscoped (hard) rules.
- **training_memory** `context`: Optional conversation context. Surfaces keyword-matched soft rules in addition to the always-firing hard rules.
- **memory_curate** `about`: person = what the user said, wanted, decided or asked, or who they are. machinery = the state of a system (tool, harness, service, key, build, project phase, error, version) — refused; read it live.
