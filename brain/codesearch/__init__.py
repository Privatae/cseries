"""
In-pod per-user code search — a pgvector + tsvector hybrid engine in the Host's
own Postgres, ported from the platform's code index. Single-tenant:
runs inside the entity pod, indexes the entity's OWN named projects, and routes
embed AND rerank through the entity's CONFIGURED lane — the bridge (the
sanctioned path to the GPU) only when no BYO/bastion backend is set. See
bridge_embed: a configured BYO/bastion lane means rerank is skipped rather than
sent to the platform, because rerank carries raw chunk content.

  store    — pgvector (vectors) + tsvector (lexical) in code_chunks, namespaced by project
  (more modules land here as Phase 2 proceeds: bridge_embed, searcher, indexer)
"""
