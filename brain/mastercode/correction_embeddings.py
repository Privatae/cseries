"""Dim→column routing for behavioral_corrections.trigger_embedding.

The constitutional similarity lane stored trigger vectors in a single
vector(1024) column, so an entity whose configured embedding model emits
any other dim wrote NULL and the lane went inactive. Mirrors entity_memory's multi-dim column scheme
(memory_engine.EMBEDDING_COLUMN_BY_DIM): one column per catalog dim,
halfvec above pgvector's 2000-dim HNSW/vector comfort line, and the
literal always cast with embedding_cast_type(dim).

Writers (behavioral_correction_digest.record_correction,
correction_writer) and the reader (constitution/context.py) MUST route
through this table — a hardcoded `trigger_embedding ... ::vector` is the
bug this module removes. Columns exist per init-db.sql +
migrations/v101_trigger_embedding_multidim.sql.
"""
from __future__ import annotations

TRIGGER_EMBEDDING_COLUMN_BY_DIM: dict[int, str] = {
    768:  "trigger_embedding_768",   # vector
    1024: "trigger_embedding",       # vector — original column (init)
    2048: "trigger_embedding_2048",  # halfvec
    2560: "trigger_embedding_2560",  # halfvec — qwen3-embedding:4b
    4096: "trigger_embedding_4096",  # halfvec
}


def vec_dim(vec: object) -> int:
    """Dimension of a pgvector literal like '[0.1,0.2,...]' or of a raw
    list/tuple of floats. 0 for empty/unknown shapes."""
    if not vec:
        return 0
    if isinstance(vec, (list, tuple)):
        return len(vec)
    if isinstance(vec, str):
        return vec.count(",") + 1
    return 0


def trigger_column_for(vec: object) -> tuple[str | None, int]:
    """(column, dim) for a pgvector literal or float list; (None, dim)
    when no column exists for that dim — callers store NULL / skip the
    similarity lane loudly rather than mis-embed."""
    dim = vec_dim(vec)
    return TRIGGER_EMBEDDING_COLUMN_BY_DIM.get(dim), dim


def trigger_cast_type(dim: int) -> str:
    """SQL cast for the trigger column of `dim` — must match the column's
    pgvector type or both assignment and `<=>` fail (same rule as
    memory_engine.embedding_cast_type)."""
    return f"halfvec({dim})" if dim > 2000 else f"vector({dim})"
