---
name: embedding
description: How to explain, set up, and switch the host's embeddings — always an endpoint the user provides (their own machine, or a provider they hold a key for). There is NO platform embedder. Plus coverage + re-embed jobs. Never billed per call by Privatae.
allowed-tools: embedding_route, embed_jobs, gitea
---

# Embedding SOP

The handler asked about embeddings, or a search came back "embeddings not set
up yet". Explain the state, set embeddings up, switch where they compute, or
fill/re-embed coverage. Use the `embedding_route` and `embed_jobs` TOOLS (never
raw HTTP or SQL).

This covers **text, pictures and video**. They are two roles, set up the same
way: `embedding` for words, `media_embedding` for pictures. Both point at an
endpoint the user provides — a model on their own machine, or a provider they
hold a key for. Neither has a default.

## The one fact that shapes every sentence

**There is no platform-wide embedding system.** Privatae does not run an
embedder for hosts — not for text and not for pictures. Embeddings compute on
an endpoint the USER provides, and until they provide one this host has none. So:

- A host with no embedding model is **not broken and not in an outage.** It is
  a setup step the user has not taken yet. Say so plainly ("Embeddings aren't
  set up yet for this host"), and when their request needs search by meaning,
  set it up through one of the two lanes below (the default is to do it, as
  below); which lane, when it is theirs to choose, goes in the question box.
- Never describe missing embeddings as something the platform runs that has
  failed: no shared embedder that is "down", no "system-wide" anything, no
  fallback route that Privatae operates. None of those exist, so none of them
  can be broken.
- Only when a model IS configured and its endpoint stops answering is it a
  fault — and then it is the user's endpoint (their machine or their
  provider), which the reason string names.

**Privatae never bills embeddings** — no per-call charge on any lane. A
provider lane bills on the user's own provider account, as their key does for
everything else.

## The two lanes (both the user's)

- **Their own machine (the sovereign lane).** A box the user owns (their GPU)
  running its own embedding model (e.g. qwen3-embedding). Embeds compute on
  their hardware, direct over the mesh — the platform never sees the text.
  Set up with `embedding_route op=bastions` → `op=use_bastion`.
- **A provider they hold a key for.** Any keyed provider that serves embedding
  models (OpenAI, Together, DeepInfra, Fireworks…). Set up in the
  **Orchestrator → Memory** step, where the user picks provider → model; the
  key is brokered from the Keys app and never reaches the embed layer.

Models live side by side in per-dim columns. Switching WHERE embeds compute
(or which model) **never throws away existing vectors.** Backward-compat is
sacred: NEVER dump, migrate, or re-embed the user's existing vectors unless
they explicitly ask; a re-embed only ADDS a missing dim.

## Explain / inspect

- `embedding_route op=current` → the configured provider/model/dim, or
  `configured: false` — which means "not set up yet", and setting it up is the
  next step: which endpoint it uses (their machine, or a provider they hold a
  key for) is the user's choice, asked in the question box (`escalate`).
- `embedding_route op=dims` → which dims this host's memory can store + the
  active column.
- There is no catalog op. The only catalogs that matter are what the user's
  machine serves and what their keyed provider serves.

## Set up embeddings on the user's own machine

**The default is to do it, not to offer it.** A user who connected a machine
that serves an embedding model has already asked. When `op=current` says
`configured: false` and a machine is embed-capable, set it up in the same
turn and say what you did — never "say the word and I'll point it there".
(The host also does this on its own the moment such a machine checks in, so
by the time you look it may already be done; `op=current` tells you.)

1. `embedding_route op=bastions` → the user's ONLINE machines that can serve
   embeddings (advertise the `embed` capability), each with `embed_models`:
   the embedding models it actually serves, with dims. If empty, their machine
   isn't running an embedder yet — tell them to start one (ollama on the box,
   bound to its mesh interface); it appears here once it heartbeats `embed`.
   If they have no machine enrolled at all, the Getting Started walkthrough's
   Network step explains a bastion and walks them through standing one up.
2. `embedding_route op=use_bastion server=<hostname>`. Omit `model`: a host
   with no model yet gets the one the machine serves; a host that has a model
   keeps it (only WHERE it embeds moves). Name a model only from that
   machine's `embed_models` — never one you remember from elsewhere. The call
   is probe-gated: the box must actually answer for that model, or the switch
   is refused with the reason and the list of models it does serve.
3. Then `embed_jobs op=enqueue target=entity_memory` and `target=user_context`
   so existing memories and documents get vectors (the automatic path
   enqueues both for you and posts a notice to the Support card in Circles).
   Only ask which machine when there is more than one to choose from.

## Set up embeddings on a provider

Point the user at **Orchestrator → Memory**: pick a provider they hold a key
for, then one of its embedding models. The route is probe-gated (a real embed
proves the model and dim) and persisted as the host's embedding config; from
then on `op=current` reports it.

## Change the model

- On the user's machine: `embedding_route op=use_bastion server=<hostname>
  model=<id>` — the same probe-gated call; a pointer change over multi-dim
  storage, never a re-embed. Confirm name and dim first.
- On a provider: the user changes it in **Orchestrator → Memory**. There is no
  tool op for it.
- Don't change models "for no reason." There is no `switch` op and no
  `use_managed` op any more — both consulted a platform catalog that serves
  nothing, so every call either failed or silently pointed embeds at the
  platform bridge.

## Pictures and video

A picture the user uploaded is stored as-is and findable by its FILENAME from
the moment it lands. Making it findable by what is IN it needs a second role,
`media_embedding`, pointed at a model that embeds images — the same two lanes
as text: a model on their own machine, or a keyed provider that serves one.

- Unset means **not set up yet**, with the identical wording as text: "picture
  search isn't set up yet for this host — want to set it up?" It is never an
  outage, and a picture is never "broken" for lacking a vector.
- The picture model is SEPARATE from the text model and cannot be inherited
  from it. Two models produce two different vector spaces even at the same
  width, so pointing pictures at the text model would return confident
  nonsense. The host refuses rather than doing it.
- Setting it is probe-gated three times over, and each refusal names its own
  reason: the endpoint must **read pictures, not files** (it is handed the same
  image encoded two different ways and has to answer the same — a text model
  given a picture embeds the encoded bytes and gives two answers); it must
  **actually look** (a different picture must change the answer, which catches
  an endpoint returning one fixed vector); and the same model's **text tower
  must answer at the same width**, or typed words could never reach pictures.
- **On the machine lane, say what has to be on the box.** The host reaches a
  machine's models at that box's Ollama port in an OpenAI-shaped `/embeddings`
  call, and there is no way to tell it a different address. So a machine serves
  picture search only if it runs an IMAGE-embedding model there. A box running
  only a text embedder is refused by the first gate above, with a reason that
  says so — the keyed-provider lane is the way instead (set it up when the
  request covers it), never sending the user to debug their address.
- Coverage reports pictures separately from text: "N pictures searchable, M
  not". The two are never added together.

**Video is not searchable by content yet.** A clip is stored and findable by
filename. Pulling frames out of it needs a decoder on the USER's own machine —
that is their tool to run, not something the platform does — and that lane is
not built. Say that plainly; do not imply a clip is broken or that something
failed. A video-native embedding endpoint (one that takes the file whole) is
the other way it becomes searchable, and it needs no decoder at all.

## Coverage + re-embed / fill

If memories landed without a vector (embeddings were not set up yet, or the
endpoint was briefly down) or the model changed, fill/re-embed with
`embed_jobs`:

- `embed_jobs op=enqueue target=entity_memory` → first-fill the active dim for
  unembedded memories.
- `embed_jobs op=enqueue target=entity_memory_reembed target_model=<model>` →
  re-embed onto another model.

Jobs are durable + crash-safe and report progress. Don't re-embed without a
reason — on a provider lane each row spends the user's own provider credit;
on their machine it is free.

## A repository the user granted you

Every repository you can read on the forge — the ones you own and the
ones the user granted you (Gitea app → Permissions) — is in your code index
without anyone asking (the rule: if read access, index). Nothing to
ask and nothing to enqueue: a pass in your pod lists what gitea says you can
read, clones each repo as you (your own gitea identity), indexes its default
branch, re-indexes what changed when it moves, and drops the index when the
access goes. A new grant is picked up within minutes; `search_my_code` finds
it, each hit naming its repo as `owner/name`.

- `embed_jobs op=enqueue target=gitea_repo repo=<owner/name>` indexes one
  readable repo NOW instead of at the next pass.
- `search_my_code` lists, under `not_fully_indexed`, any readable repo it is
  not seeing whole — too big for the room on your disk (its files as they
  unpack, plus the database rows its index takes), a clone that failed, a
  grant not indexed yet, or no embedder answering yet (keyword search still
  works on it) — with the reason. Say so rather than reading a thin answer as "the code is not there".
- A foreign repo — on GitHub, or another user's that was not granted to you —
  is never indexed in your pod (the enqueue refuses it). Read it with the
  `gitea` / `github` tools, or dispatch an agent to read it.

## When to refuse / confirm

- A model the user's endpoint does not serve → say it isn't available there.
- A provider-lane re-embed over a large corpus → estimate rows × avg tokens at
  the provider's price and ask before proceeding.
- "Delete my embeddings" / drop a dim → confirm explicitly; memories without a
  vector are invisible to semantic search, and other-dim vectors must be
  preserved.


## embedding_route — guidance

Inspect or set how the host's MEMORY/CONTEXT is embedded + searched — the
ONE field the memory engine uses. THERE IS NO PLATFORM EMBEDDER: embeddings
compute only on an endpoint the user provides — their OWN machine (a box they
own running its own model, e.g. qwen3 on their GPU; embeds compute on their
hardware over the mesh) or a PROVIDER they hold a key for (set up in
Orchestrator → Memory). A host with no model configured is NOT broken and NOT
in an outage — it is a setup step the user has not taken yet; say
"embeddings aren't set up yet, want to set them up?" and never describe it as
down or system-wide. Switching lanes KEEPS existing vectors (NO re-embed).

- `op='current'` (default): configured provider/model/dim, or
  configured=false (= not set up yet).
- `op='dims'`: which vector dims this host's memory can STORE + the active
  dim/column (a model whose dim has no column can't be assigned).
- `op='bastions'`: the user's ONLINE machines that can serve embeddings
  (advertise the 'embed' capability) — DISCOVER where the user can run
  embeddings; pass a returned hostname as `server`.
- `op='use_bastion'`: set up embeddings on the user's machine — route the
  host's embeds to `server`. Omit `model`: a host that already has a model
  KEEPS it (just moves the lane); a host with NO model yet gets the embedding
  model the machine serves, picked from its own heartbeat list. Pass `model`
  only to choose among the models op='bastions' lists for that machine
  (embed_models) — never a name from memory; the call is probe-gated and a
  refusal names what the box does serve. This is ALSO how the model is
  changed on a machine.

DEFAULT BEHAVIOUR: when op='current' says configured=false and op='bastions'
returns a machine, call use_bastion right away — the user connected that
machine for this; do not ask first. (The host also does this on its own when
the machine checks in.) It never re-embeds or dumps — storage is multi-dim,
so a switch is a pointer change; use op='dims'/embed_jobs to fill a new
model's column if you want. Changing model or provider on a PROVIDER lane is
done by the user in Orchestrator → Memory; there is no op for it here. The
former ops catalog / switch / use_managed are retired: they consulted a
platform catalog that serves nothing.

## embed_jobs — guidance

Use `coverage` to see the gap, `enqueue` to fill it, `list`/`get` to watch
it. `entity_memory_reembed` moves memories onto another model and requires
target_model. `gitea_repo` with repo='owner/name' clones (or refreshes) a
repository this host can read on the forge into the pod and indexes it,
so `search_my_code` with `project='owner/name'` searches it by meaning
instead of the gitea tool's keyword grep. Every repo you can read is already
indexed by a background pass, with nothing to ask (the section "A repository
the user granted you" above); `enqueue target='gitea_repo'` only indexes one
NOW instead of at the next pass.


## Parameter notes (full text of shortened argument descriptions)

- **embedding_route** `op`: current = active config (default; configured=false means not set up yet); dims = storable dims + active; bastions = the user's embed-capable machines; use_bastion = serve embeddings on one.
- **embedding_route** `model`: use_bastion: optional — one of the machine's embed_models (listed by op='bastions'). Omit: a configured host keeps its model, an unconfigured one gets the machine's.
- **embedding_route** `dim`: For op='use_bastion' with a new 'model': optional output dim (768/1024/2048/2560/4096); defaults to the model's known dim.
- **embed_jobs** `target`: enqueue: entity_memory / user_context = fill missing embeddings; entity_memory_reembed = re-embed to target_model; gitea_repo = clone + index 'repo'.
- **embed_jobs** `repo`: For target='gitea_repo': the repository as 'owner/name' on the forge (one the host owns or was granted; gitea op='list' shows them).

