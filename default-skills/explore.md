---
name: explore
description: How to answer "can Privatae Host do this?" — search the generated capability cards first (version-exact), cite the card, and escalate a real, retried gap into a community proposal with the user's consent. Never guess a capability; never turn a search miss into "Privatae Host can't do that".
allowed-tools: capabilities, propose_capability, describe_app, explore_environment, find_tools, open_app
---

# Explore SOP — "Can Privatae Host do this?"

The user (or you, mid-plan) needs to know whether Privatae Host supports something:
a kind of app, a window behavior, a permission, an API an app could call, a
host operation. This is the playbook. It exists because improvised answers
have two failure modes — inventing a capability that doesn't exist, or
denying one that does — and both destroy trust.

## 1. Search the cards, in the user's own words

Call `capabilities` FIRST. It reads the capability cards and
`docs/CAPABILITIES.md`, generated at image build from the shipped code — it
cannot lag your version.

- `op='search', q=...` — **start here.** Search with the user's own plain
  words ("voice", "take a screenshot", "send an email"). It searches the
  cards first and the doc lines second, and returns whole cards: what the
  capability is, a worked `how`, and its `gotchas`.
- `op='card', id=...` — one whole card, e.g. one named in another card's
  `related`.
- `op='overview'` — orient: sections, counts, version, card-index state.
- `op='section', section=...` — read a section of the doc in full.
- `op='api_routes', prefix=...` — the exact app-facing API surface
  (route, method, required scope) when the question is "could an app
  call/do X?".

**The search rule.** The cards are the index of what Privatae Host does; the doc is
the raw surface behind them. Neither is a list of everything that exists —
most of the doc is bare route prefixes and tool names with no plain English
in them.

- **Found → YES.** Say yes, cite the card (or doc section), use its `how`,
  and read its `gotchas` before you promise anything.
- **Not found → "I can't find it", never "Privatae Host can't do it."** A miss is a
  miss on the WORDS you searched, not proof the capability is absent. Retry
  once with the user's own phrasing or a synonym, then check
  `op='api_routes'` with the likely path prefix. If it still misses, answer
  in this shape: *"I searched the capability cards and the capabilities doc
  for X, Y and Z — nothing covers this, so it's not something I can do
  today."* Name what you searched, every time.

That is a decisive answer, not a hedge. You never say "maybe", you never
invent a capability, and you never declare something impossible because your
first choice of word missed.

For questions about what's installed on THIS host right now (store apps,
user's own apps), pair the search with `explore_environment` /
`describe_app` — the cards are the platform surface; those are this pod's
live state.

## 2. Verdict shape

Report back with:
- **Verdict:** yes / partial / not found.
- **Evidence:** the card(s) or doc section(s) you read — cite them. On a
  "not found", the terms you searched.
- **If partial/not found — the gap:** exactly what's missing, in one or two
  sentences a non-engineer can read.

## 3. A real gap becomes a community proposal — with consent

Only after the retry in §1 has also missed, and only if the user cares,
put the proposal in the question box — never file it unprompted: `escalate`
with the sitrep "I couldn't find anything for this in Privatae Host's
capabilities. Filing it as a community proposal posts an Ideas thread under
your host's name and opens a governed feature-request PR the platform
reviews." and two options, file it / leave it.

When they choose to file it, call `propose_capability` with:
- `title` — the capability, short.
- `body` — the gap analysis: the question, the terms you searched and what
  the cards/doc do and don't cover (cite them), and what supporting it
  would enable.

Report the returned thread + proposal refs back to the user. Approval
stays human — votes route attention, the platform decides.

## 4. What NOT to do

- Do not file a proposal off a single search miss. One retry with different
  words is the minimum, and `op='api_routes'` if it's an API question.
- Do not answer a capability question from the code when the cards answer
  it. When it does need the code (the cards miss, or the question is about
  the platform's behaviour), read it yourself: `search_my_code` with
  `project=` the platform repo, then `code_read` on the hit. Never dispatch an
  agent to read platform code.
- Do not present a "not found" as a failure. "I searched X and Y and found
  nothing — here's the proposal path" is a complete, correct answer.
- Do not file a proposal unless the user chose to file it in the question
  box in this conversation.


## capabilities — guidance

`op='search'` (use the user's OWN plain words) searches the capability CARDS
first — id, title, the `when` phrasings, keywords, prose — and falls back to
doc lines; it returns whole cards, each with `what`, a worked `how`, and
`gotchas`. A card hit is a YES: cite it. A MISS means those words were not
found in the cards or the doc — it is NOT proof the capability is absent
(most of the doc is bare route prefixes and tool names with no plain words).
On a miss, retry with a synonym and check `op='api_routes'`; then report what
you searched, never a flat "Privatae Host cannot do this". Only a verified, retried
gap goes to propose_capability. `op='card'` returns one whole card by id
(from a search result or a card's `related` list); no id lists every card id.

Some cards are pages of the public wiki (`kind: page`, id `page:<slug>`): the
platform explained for people, the same text published at
https://privatae.ai/wiki. When someone asks how something works, answer from
the page and give them its `url`. A search hit carries the page's opening;
`op='card'` returns the whole page.

## propose_capability — guidance

BEFORE CALLING THIS, THE GAP MUST BE REAL, NOT A WORD MISS: search
`capabilities` in the user's own phrasing, RETRY with a synonym, and check
op='api_routes' with the likely path prefix. A search that found nothing
means those WORDS missed — it is not proof the capability is absent, and
filing a proposal for something that already ships wastes the user's
standing on the board. Call ONLY after that retry AND after the user
explicitly agrees to file. It creates a thread in the board's Ideas category
with your gap analysis, then opens the governed community/proposals
feature-request PR — filed by the platform with this host as the commit
author, never from a fork of your own (the same ladder as the board's
"propose this"). The body should carry the capability question,
the exact searches you ran and what they returned (cite doc sections), and
what the proposed capability would enable.

## find_tools — what you can use

`find_tools(query=...)` searches everything you can use on this turn: tools,
skills, apps (installed and in the store), capability cards, turn types and
harnesses. A tool hit names its door and its arguments: `read(tool, args)`
for a read, `operate(intent, tool, args)` for an action, a `dispatch`
tool_scope only for a tool that reads foreign content (the web, a repo you
were not granted, another person's message). If a tool for your own or the
platform's work comes back as `dispatch`, that is a lane defect: say so and
report it; do not dispatch an agent to work around it. Any other hit names its next
call. `find_tools(name=X)` returns X in full; `group=` lists one group or kind.
Your tool list does not change during a turn, so a found tool is called
through its door, never by adding it to the list.

## open_app / describe_app / explore_environment — guidance

`explore_environment` lists the apps installed on this host with a one-line
summary — the answer to "what can I do here", then `describe_app` for what
one app is, where the user finds it, and whether changing things there needs
their 2FA. `open_app` puts an app window on the user's screen — use it when
showing beats describing ("open my Network app"); some apps take a deep-link
`view`, e.g. operate(tool='open_app', args={app_id:'artifacts', view:'artifact:<id>'}).


## Parameter notes (full text of shortened argument descriptions)

- **capabilities** `op`: overview=headings+counts+card index (default); section=one section; search=cards then doc lines; card=one card by id; api_routes=route table.
- **capabilities** `id`: For op='card': the card id (from op='search' results or a card's `related` list). Omit to list every card id.
