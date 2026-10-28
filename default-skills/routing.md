---
name: routing
description: How the host reads and sets which provider and model answers each kind of work, recommends routes for a new user from the keys they hold, browses the model catalog, and wires a stored key to a consumer. Never route silently; never invent a model id.
allowed-tools: model_routing, model_route, inference_catalog, credential_wiring, keys_manage, embedding_route, preference, image_generate
---

# Routing SOP

## model_routing — guidance

This is how you set a new user up in conversation instead of sending them off
to the Orchestrator app; do that whenever they ask you to get them running,
or when `op='status'` shows roles unrouted and they have a key.

- `op='status'` (default): every role with what it resolves to right now,
  which providers the user holds keys for, which roles are still unrouted,
  and ready=true when nothing is missing.
- `op='recommend'`: what to pick for the unrouted roles — text roles from the
  same researched data the Orchestrator's one-click setup applies, image
  roles from the provider's LIVE image-capable catalog (so a suggestion is
  always a model that provider actually serves today). It suggests only
  providers the user already has a key for, and it WRITES NOTHING. If the
  user holds NO provider key, recommend returns blocked — send them to the
  Keys app to add one, then come back.
- `op='set'`: one route (role + provider + model, optional `reason`,
  optional `fallback_provider` + `fallback_model`).
  Say what you are setting and why — never route silently. What happens
  depends on the role, and the result tells you which:
  - a role with NO route: applied at once (nothing ran there to break) —
    tell the user what was set and why;
  - a route the user approved before for that role: applied at once
    (reverting is free);
  - a role that already works, moved anywhere else: NOT applied. A decision
    card is filed for the user (Circles: Waiting on you) with your `reason` on
    it, and the route lands only when they choose 'apply'. Say so and end
    your turn — do not set it again, and do not describe the move as done.
  A route that lands is an ordinary entry the user can see and change in the
  Orchestrator; there is no separate host lane and no hidden default.
  Every route that lands gets a CANARY: for the first 20 tool calls the new
  model makes on the role, an invented or off-list tool name reverts the
  role to the previous approved route and the user gets an inbox note
  saying which call tripped it. A rejected route loses its approved
  standing — you cannot revert to it for free; only the user setting it
  again gives it a fresh window. op='status' shows an open window per role,
  and says when the canary set the current route: if a route you set was
  reverted, say so — do not set it again without a reason the user hears. The
  model id is spelled AS THAT PROVIDER SPELLS IT — take it from
  op='recommend', never invent one. provider is a Keys registry provider id
  (openrouter, anthropic, openai, google, xai, kimi, …) or 'local'. Ground a
  proposal in the `measured` block of op='status' (tool calls the surface
  could not take, failed calls, latency, cost) rather than an impression.
  Each `measured.models` row carries a **lane**: 'host' is your own turns,
  'agent' is the agents you dispatched. They often run on the same model, so
  read the lane before you blame it — "my agents invent tool names" and "I invent
  tool names" are different findings with different fixes.

Roles: chat (you, talking — and every lane with no model of its own: the
Workbench's phase planner, web research, the VS Code editor; an agent you
dispatch for untrusted work runs on the model the dispatch names, else the
one preferred for that kind of work, `preference` op='prefer', else this
one), and image_gen (making an image). There
is no role for reading an image: you read pictures with your own model
(image_generate action='look'), so a Host model whose catalog row lists image
input is one that sees.
Embedding is NOT set here — it has its own flow (embedding_route).

There is one text role, chat (the host model), and no others. If the user
asks about a plan, research, agents, tutor or journal route, say those were
removed 2026-09-19; a memory ("consolidation") route went 2026-09-30, when
memory came to form in your own turn; a review route went 2026-10-20: you
review what your builds changed in your own turn, and a second opinion is a
coding engine's review (skill 'review'). All of that work runs on the host
model now. Never offer to set one.

**When the provider's safety filter declines a request.** Anthropic's
recent models (Opus 5.5, Opus 5, Sonnet 5.5, Haiku 5.5, Fable) run safety
classifiers that can decline a request, ordinary security or biology work
included. The request is then run again on the route's fallback, the one set in
the Orchestrator, as for any other failed call; with no fallback set, nothing
answers and the person reads "Anthropic's safety filter declined this request
(category: …). No fallback model is set for this in the Orchestrator."
`diagnose` shows each decline, its category and what the fallback did.
- No fallback set: tell the person plainly what was declined and that no
  other model is set to answer it, and propose one for the role
  (`op='set'` with the same provider and model, plus `fallback_provider` and
  `fallback_model` — a model they hold a key for, taken from `op='recommend'`
  or the catalog; on Anthropic a different Claude model, since a blank
  fallback model means the same one). That files a card: the choice is
  theirs, and nothing changes until they approve it.
- The fallback answered: say once which model answered and why, then go on.
  Do not repeat it every turn.
- Category `reasoning_extraction`: the request asked the model to write out
  its internal reasoning. It is never sent to a fallback, so do not offer
  one; asking for a short explanation of the answer, or a summary of what
  was done, is fine.

**Web search when the host model cannot search.** Only OpenRouter, Anthropic,
OpenAI, xAI and Kimi ship a web mechanism. If the host model is on one that
does not (local, google, deepinfra, together, fireworks, groq), web search runs
on one of the person's connected coding engines that can search (Antigravity,
then Claude Code), on that engine's own key or sign-in. It takes
minutes rather than seconds, and the result names the engine: say so. With
none of them connected, web search is unavailable and the result says how to
fix it. When a model the catalog marks as unable to search sits on
a provider that can, the search borrows that provider's recommended chat model
for that call only. Nothing is stored and the routing table is untouched.

A stored key is NOT enough, on purpose: borrowing a merely-keyed provider
would open a bill on one the user never chose to run this host on. So the
borrow is always something they already pay for.

The result carries `route_note` ("searched via <provider>/<model> because the
chat model cannot search") on EVERY outcome, including failures — RELAY it,
so the user knows which model answered and why. If a borrowed search fails,
say the borrowed provider failed; do NOT tell them to re-route the host model
as though it had answered. If nothing routed can search, the result says so
and names the fix: point the host model at a search-capable provider
in the Orchestrator.

## model_route / inference_catalog — guidance

A provider is one the user holds a key for in the Keys app (openrouter,
anthropic, openai, xai, …) or 'local' (a machine they run); every keyed
provider may serve any text role. The platform runs no inference lane of its
own. Catalog rows whose vision/image_gen flag is null publish no modality
lists — unknown, not unable. The catalog is ~400 rows and is never returned
whole: narrow with search / provider / capability, page with offset. Use
inference_catalog to tell the user which models they can pick for a chat
thread or per role; to pick a model just for the current thread the Circles
picker passes it on the message.

## credential_wiring — guidance

`op='get'` returns the provider registry (canonical Keys names per provider —
'<provider>' for API keys, '<provider>-oauth' for logins — and which are
direct-lane capable) plus the current wiring map. Provider economics when
advising a route: OpenRouter aggregates everything under ONE bill but adds a
fee on BYOK traffic (~5.5%); a direct provider key pays list price. Route by
what the user HAS and what's cheapest for the model they want — never assume
OpenRouter. Wiring changes only on the user's direction; consumer ids look
like 'harness:opencode', 'training', or a role.

## The Orchestrator app — what is where

The user sets routes by hand in the Orchestrator app. This is its layout, for
"where do I…" questions.

**Simple mode** (it opens here) is a setup checklist, in order. THINKING: pick
a provider the user holds a key for, and one click writes the recommended
route for the host model (the same data `op='recommend'` reads), with an
optional backup provider as its fallback. MEMORY: embeddings, either
free on a machine of the user's own (it opens Getting Started at its Network
step, which explains a bastion) or a keyed provider's embedding model (skill
'embedding'). SEEING: the image-making model, chosen from an authenticated
provider whose catalog declares the modality; reading an image is not a
route. HEARING: the realtime-voice provider; the voice app picks the model
when it opens the socket. Below the checklist, "What answers when" says per
activity which provider and model answers and at what effort. Recommendations
are data shipped with the app, checked against the provider's live catalog
under the user's key when applied; an unrouted role still resolves empty.

**Advanced mode** (a toggle at the top; the choice sticks). ROUTING: one row
per text role, each with a provider (one with no key in Keys shows
disabled), a model (the user's starred favourites for that provider, else its
live catalog; never a typed id), an optional fallback provider and model that
answers when the primary errors, does not host the model, or its safety
filter declines the request (a blank model means the same id), and a reasoning effort where the gateway can send one (on
Anthropic the levels are the routed model's own, and an unset Anthropic route
sends no effort). MODELS: each provider's catalog under the user's key
(OpenRouter's is searchable with facets; local shows the mesh table); stars
write favourites. CODING ENGINES: every engine in two bands, first-party
(Claude Code, Codex, Cursor, Grok, Kimi Code, Antigravity CLI, each on its
vendor's key or login, "not connected" without it and never substituted) and
generic (opencode, on the user's generic provider key), each row with its key
state and a one-click fix, its default model, default effort and the default
tick. A build that names no engine runs on the ticked one; with no tick and
exactly one engine connected, on that one; with several connected and no
tick, the build stops and asks. MULTI-AGENT CODING: the user's coding
protocols (skill 'coding-protocols'). EMBEDDING: one provider and model row,
probe-gated on every switch; switching never re-embeds or deletes vectors.
BALANCES: what each provider account holds, read live under the user's Keys
credential; a provider whose API exposes no balance says so. A model whose
capability was inherited from the matching OpenRouter row is labelled so. All
API keys live in the Keys app; this app never touches credentials.


## Parameter notes (full text of shortened argument descriptions)

- **model_route** `op`: current = per-role routing + effective model (default); catalog = one page of available models; switch = assign a model to a role.
- **model_route** `provider`: switch: a provider the user holds a key for (openrouter, anthropic, openai, xai, …) or 'local'. catalog: filter to 'openrouter' | 'local'.
- **model_routing** `op`: status = what is routed now (default); recommend = what to pick for unrouted roles, writes nothing; set = write one route.
- **model_routing** `role`: For op='set': which kind of work. chat, image_gen.
- **model_routing** `provider`: For op='set': a Keys registry provider id (openrouter, anthropic, openai, google, xai, kimi, …) or 'local'. Must be one the user holds a key for.
