---
name: user-documents
description: How to find, read, and work with the documents the user uploaded (CSVs, statements, notes) — the context corpus. Two calls to read a file, never a hunt; memory stores conclusions, never the raw document.
allowed-tools: context_ingest, embed_jobs, recall
---

# User Documents SOP

The user asked about a file they uploaded, or you need one to answer. This is
the playbook. It exists because two failure modes destroy trust: flailing
through dozens of tool calls hunting for a document, and answering "I can't
find it" from the WRONG store.

## One search, every store

`recall` is THE search verb: one call covers your memory, the user's
uploaded documents, their projects, indexed code, skills and your own database,
grouped and labeled by store (chat turns live in your memory, so
conversations surface there — reopen one with `chat_threads`); `scope`
narrows it to one store. For a file the user uploaded, the documents store
is the one that answers. Each memory and documents line says HOW it was searched — meaning+words, or words-only
when embeddings are not set up yet or their endpoint is not answering — and
an UNAVAILABLE source is named explicitly. "Not set up yet" is a setup step
for the user, never an outage: setting embeddings up is the next step (the
`embedding` skill; which endpoint is theirs to choose in the question box), do
not report a broken system. Unavailable is never empty: a real "no matches" only comes from
a search that ran.

| Source | What's in it | Read/act verbs |
|---|---|---|
| User's documents | Files the user uploaded | `context_ingest` op='files'/'read' ('search' for doc-only scope) |
| Your memory | What YOU concluded and experienced | `context_ingest` op='promote' internalizes a conclusion |
| Chat history | What was said, and when | `chat_threads` list/episodes/read |

## Reading a document = exactly two calls

1. `context_ingest op='files'` — the corpus by filename. Find the file the
   user means (optionally filter with `query`).
2. `context_ingest op='read', file='<name>'` — the WHOLE document, chunks in
   upload order, newest copy automatically. Long files paginate: the result
   says "page X of Y" — keep reading pages until you have what you need.
   The result flags older copies if the user re-uploaded; pass
   `upload_batch` only when you need a specific older copy.

Not listed in op='files'? Then it was NOT uploaded (or was superseded).
Say so and ask the user — never dispatch an agent to hunt for a file.

Identifiers you were HANDED (a filename or batch key inside a task prompt,
a key copied out of an old note) are transcriptions — relay abbreviates and
mangles them. op='read' resolves near-misses itself and says so
(`file_note`/`batch_note`); a read error lists the real filenames. Treat an
exact-match miss as "my reference is wrong" first — only op='files' showing
the corpus without the file proves absence.

`op='search'` is for passages by MEANING across the corpus ("what did I pay
the insurer in May") — use it to locate, then `read` the file for full fidelity.
Note: search only matches embedded pieces; `embed_jobs op='coverage'` tells
you if part of the corpus isn't embedded yet (and op='list' shows running
jobs). An unembedded file still reads fine with op='read'.

## Memory discipline — conclusions, not ledgers

NEVER copy a raw document into your memory, and never dispatch an agent to
"internalize" a file — that pattern is dead. The document IS the source of
truth and stays in context, where you re-read it fresh (the user may replace
it). What belongs in your memory is what you CONCLUDED: the classification
you built, the reconciliation you did, the decision the user made — stored
with a pointer to the source filename so future-you knows where the evidence
lives.

## Times

Timestamps on context rows, jobs, and memories are UTC. Speak times in the
user's timezone (your Communication Context block carries it). Compute
"today" and "yesterday" in THEIR zone, never UTC.


## context_ingest — guidance

This is where documents LIVE: read them fresh here whenever needed (the user
may replace them) — do NOT copy documents into your memory or dispatch agents
to "internalize" files. Memory is for CONCLUSIONS you draw from them; promote
is for a single worthwhile entry, never a whole document.

- `op='stage'` (default) stages ONE piece — content (required), plus optional
  title/source/source_id/tags/metadata/created_at, and embed=true to embed it
  inline. `op='bulk'` stages many at once — items: a list of the same
  per-item shape (use for an import/export dump).
- `op='search'` semantic-searches staged context — query (required), limit
  (default 20).
- `op='files'` lists the corpus FILE-level (uploaded docs roll up by
  filename) — use it to find what the user uploaded; optional query filter,
  page/per_page. It lists the USER'S uploads: tool results you stashed for
  your own recall (source 'tool:<name>') are excluded by default and are
  never the user's documents — pass include_host_staged=true to see them
  too, labelled host_staged.
- `op='read'` reads one uploaded document WHOLE, chunks in upload order —
  file (the filename from op='files') and/or upload_batch, page/per_page
  (paginated honestly: the result says "page X of Y", request the next page
  to continue). This is how you READ a CSV/document the user uploaded —
  search gives fragments; read gives the document. read forgives mangled
  references: near-miss filenames and abbreviated batch keys resolve with a
  note (file_note/batch_note), and a true miss errors NAMING the real
  filenames — such an error means "your reference is wrong", never "the
  document is missing".
- `op='promote'` internalizes a staged entry into the host's entity_memory —
  context_id (required) plus optional memory_type/importance/
  dimension_scores. `op='list'` lists staged entries — optional source/status
  filters (status='all' for no filter), page, per_page.

Everything is scoped to THIS host. For a large backfill, stage without embed
then run a durable embed job (`embed_jobs target='user_context'`).


## Parameter notes (full text of shortened argument descriptions)

- **context_ingest** `file`: read: the uploaded document's filename (from op='files'). A near-miss resolves with file_note; an unknown name errors naming the real filenames.
- **context_ingest** `upload_batch`: read: the upload batch key (with or instead of file when a name was uploaded twice); a prefix resolves, a wrong key falls back to the newest upload.
- **context_ingest** `source`: stage/bulk/list: origin system (stage/bulk default 'host' — marks the entry host-staged in the user's Context Explorer); list uses it as a filter.
- **context_ingest** `include_host_staged`: files: also list tool results you stashed yourself (source 'tool:*'), labelled host_staged=true. Default false — the user's uploads only.
