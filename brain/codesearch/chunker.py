"""
Tree-sitter chunker — AST-aware splits at function / class / top-level
boundaries, with a size-cap fallback for unsupported languages or chunks
that exceed MAX_CHUNK_CHARS.

Pattern from VectorCode (MIT). We do not use VectorCode's runtime — only
the chunking shape — because we target our pgvector store directly.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("code_index.chunker")

# Map file extensions to tree-sitter language ids understood by
# tree-sitter-language-pack.
EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".go": "go",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
    ".dockerfile": "dockerfile",
}

# Per-language node types we treat as "top-level chunkable units".
# Falls back to per-N-lines split if nothing here matches.
CHUNKABLE_NODE_TYPES: dict[str, set[str]] = {
    "python": {
        # `decorated_definition` is handled specially in _walk_top_level
        # (descend to inner function/class so the symbol field resolves).
        "function_definition", "class_definition",
        "import_statement", "import_from_statement",
    },
    "javascript": {
        "function_declaration", "class_declaration", "method_definition",
        "lexical_declaration", "variable_declaration", "export_statement",
        "import_statement",
    },
    "typescript": {
        "function_declaration", "class_declaration", "method_definition",
        "interface_declaration", "type_alias_declaration",
        "lexical_declaration", "variable_declaration", "export_statement",
        "import_statement",
    },
    "tsx": {
        "function_declaration", "class_declaration", "method_definition",
        "interface_declaration", "type_alias_declaration",
        "lexical_declaration", "variable_declaration", "export_statement",
        "import_statement",
    },
    "rust": {
        "function_item", "impl_item", "struct_item", "enum_item",
        "trait_item", "mod_item", "use_declaration",
    },
    "go": {
        "function_declaration", "method_declaration",
        "type_declaration", "import_declaration", "var_declaration",
        "const_declaration",
    },
    "sql": {
        "create_function_statement", "create_table_statement",
        "create_view_statement", "create_index_statement",
        "create_trigger_statement",
    },
}

# Heuristic for symbol name extraction within a chunk node, per language.
# Tree-sitter gives us .child_by_field_name("name") for most decl kinds.
SYMBOL_NAME_FIELD = "name"

MAX_CHUNK_CHARS = 4000   # no chunk is longer, on any path
MIN_CHUNK_CHARS = 80     # drop tiny chunks (single import etc.)
LINE_FALLBACK = 60       # lines per chunk for unsupported languages
# A line longer than MAX_CHUNK_CHARS (a minified bundle, a one-line literal) is
# cut into windows of that size, each overlapping the one before by this much,
# so a match that straddles a cut is still whole in one window.
LONG_LINE_OVERLAP = 200

# Part of every file's hash (file_hash): the indexer re-chunks a file only when
# its hash moves, so a change to how files are chunked bumps this and every
# file is chunked again once. 2 (2026-10-13): module-level code between
# definitions is indexed, every chunk is held to MAX_CHUNK_CHARS, and an
# oversized unit's sub-chunks no longer claim one line past their end.
CHUNKER_VERSION = 2


@dataclass(frozen=True, slots=True)
class Chunk:
    file_path: str
    chunk_index: int
    content: str
    language: str
    # "function", "class", "import", ... for a definition; "module" for the
    # top-level code between definitions; "file_tail" for the code after the
    # last one; "line_window" for a file with no parser; "long_line" for a
    # window of one line longer than MAX_CHUNK_CHARS.
    chunk_kind: str
    symbol: str | None
    start_line: int             # 1-based, inclusive
    end_line: int               # 1-based, inclusive
    file_hash: str              # file_hash() of the parent file's contents

    def token_estimate(self) -> int:
        # Cheap heuristic; bge-m3 averages ~3.5 chars per token for code.
        return max(1, len(self.content) // 4)


def file_hash(content: str | bytes) -> str:
    """sha256 of the chunker version and the file's contents: a file is
    re-chunked when either changes."""
    if isinstance(content, str):
        content = content.encode("utf-8", "ignore")
    return hashlib.sha256(b"chunker-%d\x00" % CHUNKER_VERSION + content).hexdigest()


def detect_language(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in EXT_TO_LANG:
        return EXT_TO_LANG[ext]
    # Special-case files without extensions
    name = Path(path).name.lower()
    if name in ("dockerfile", "containerfile"):
        return "dockerfile"
    if name in ("makefile",):
        return "make"
    return None


def _split_lines(text: str) -> list[str]:
    """`text` as its lines, each keeping its "\\n". Only "\\n" ends a line:
    str.splitlines also splits on \\f, \\x1c, \\u2028 and others, which would
    put every line after one of them at the wrong line number."""
    lines = text.split("\n")
    out = [ln + "\n" for ln in lines[:-1]]
    if lines[-1]:
        out.append(lines[-1])
    return out


def _windows(text: str, first_line: int) -> list[tuple[str, int, int, bool]]:
    """`text` (whose first line is file line `first_line`) as (piece,
    start_line, end_line, long_line) windows: at most LINE_FALLBACK lines and
    MAX_CHUNK_CHARS characters each, lines inclusive. A single line over
    MAX_CHUNK_CHARS is cut into overlapping windows of that size, each on
    that line and marked long_line."""
    lines = _split_lines(text)
    out: list[tuple[str, int, int, bool]] = []
    buf: list[str] = []
    buf_chars = 0
    buf_start = 0                       # index in `lines` of buf's first line
    for i, line in enumerate(lines):
        if buf and (len(buf) >= LINE_FALLBACK or buf_chars + len(line) > MAX_CHUNK_CHARS):
            out.append(("".join(buf), first_line + buf_start, first_line + i - 1, False))
            buf, buf_chars = [], 0
        if len(line) > MAX_CHUNK_CHARS:
            step = MAX_CHUNK_CHARS - LONG_LINE_OVERLAP
            for s in range(0, len(line), step):
                out.append((line[s:s + MAX_CHUNK_CHARS], first_line + i, first_line + i, True))
                if s + MAX_CHUNK_CHARS >= len(line):
                    break
            continue
        if not buf:
            buf_start = i
        buf.append(line)
        buf_chars += len(line)
    if buf:
        out.append(("".join(buf), first_line + buf_start, first_line + len(lines) - 1, False))
    return out


def _line_window_chunks(
    file_path: str,
    content: str,
    *,
    language: str,
    fhash: str,
    first_line: int = 1,
    kind: str = "line_window",
    symbol: str | None = None,
    first_index: int = 0,
) -> list[Chunk]:
    """Windows of `content` (_windows) as chunks numbered from first_index.
    The whole file when tree-sitter can't parse it; a gap between definitions
    (kind 'module'), the tail after the last one ('file_tail'), or a
    definition over MAX_CHUNK_CHARS (its own kind and symbol) otherwise. A
    window shorter than MIN_CHUNK_CHARS joins the one before it when the two
    fit in MAX_CHUNK_CHARS."""
    chunks: list[Chunk] = []
    idx = first_index
    for text, start, end, long_line in _windows(content, first_line):
        prev = chunks[-1] if chunks else None
        if (len(text) < MIN_CHUNK_CHARS and prev is not None and not long_line
                and prev.chunk_kind != "long_line"
                and len(prev.content) + len(text) <= MAX_CHUNK_CHARS):
            chunks[-1] = Chunk(
                file_path=prev.file_path,
                chunk_index=prev.chunk_index,
                content=prev.content + text,
                language=prev.language,
                chunk_kind=prev.chunk_kind,
                symbol=prev.symbol,
                start_line=prev.start_line,
                end_line=end,
                file_hash=fhash,
            )
            continue
        chunks.append(Chunk(
            file_path=file_path,
            chunk_index=idx,
            content=text,
            language=language,
            chunk_kind="long_line" if long_line else kind,
            symbol=symbol,
            start_line=start,
            end_line=end,
            file_hash=fhash,
        ))
        idx += 1
    return chunks


def _try_tree_sitter(language: str):
    """Lazy import tree-sitter-language-pack; return (Parser, Language) or None.

    We catch any import / language-not-supported error so an unknown language
    falls back to line-window chunking instead of crashing.
    """
    try:
        from tree_sitter_language_pack import get_parser
        return get_parser(language)
    except Exception as e:
        log.debug("tree-sitter unavailable for %s: %s", language, e)
        return None


def _name_of(node, source_bytes: bytes) -> str | None:
    """The declared name of a node: its own `name` field, or for a JS/TS
    `const`/`let`/`var` the first declarator's name (the declaration itself
    carries none)."""
    name_node = node.child_by_field_name(SYMBOL_NAME_FIELD)
    if name_node is None and node.type in ("lexical_declaration", "variable_declaration"):
        decl = next((c for c in node.named_children if c.type == "variable_declarator"), None)
        if decl is not None:
            name_node = decl.child_by_field_name(SYMBOL_NAME_FIELD)
    if name_node is None:
        return None
    return source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", "ignore")


def _kind_and_symbol(node, source_bytes: bytes) -> tuple[str, str | None]:
    """An `export` wraps the declaration that carries the name and the kind:
    `export function placeProject` is a function named placeProject, not an
    unnamed export_statement. `export default <expression>` names nothing, so
    it is recorded as 'default'."""
    if node.type == "export_statement":
        inner = node.child_by_field_name("declaration") or node.child_by_field_name("value")
        if inner is not None:
            name = _name_of(inner, source_bytes)
            is_default = any(c.type == "default" for c in node.children)
            return inner.type, name or ("default" if is_default else None)
        return node.type, None
    return node.type, _name_of(node, source_bytes)


def _walk_top_level(root_node, source_bytes: bytes, chunkable: set[str]):
    """Yield (node, kind, symbol) for top-level chunkable nodes."""
    for child in root_node.children:
        if child.type in chunkable:
            kind, symbol = _kind_and_symbol(child, source_bytes)
            yield child, kind, symbol
        elif child.type in ("decorated_definition",):
            # Decorated python defs — descend one level
            inner = next((c for c in child.children if c.type in chunkable), None)
            if inner is not None:
                name_node = inner.child_by_field_name(SYMBOL_NAME_FIELD)
                symbol = None
                if name_node is not None:
                    symbol = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", "ignore")
                yield child, inner.type, symbol


def _kind_label(node_type: str) -> str:
    """Normalise verbose tree-sitter node types to short labels."""
    if "function" in node_type or node_type in ("method_definition", "method_declaration"):
        return "function"
    if "class" in node_type:
        return "class"
    if "import" in node_type or "use_declaration" in node_type:
        return "import"
    if "interface" in node_type:
        return "interface"
    if "type_alias" in node_type or "type_declaration" in node_type:
        return "type"
    if "struct" in node_type:
        return "struct"
    if "enum" in node_type:
        return "enum"
    if "trait" in node_type:
        return "trait"
    if "impl" in node_type:
        return "impl"
    if "lexical_declaration" in node_type or "variable_declaration" in node_type:
        return "variable"
    return node_type


def chunk_file(file_path: str, content: str) -> list[Chunk]:
    """Chunk a file. Returns possibly-empty list of Chunk."""
    language = detect_language(file_path)
    fhash = file_hash(content)

    if not content.strip():
        return []

    # Markdown / plain text / unknown languages get line-window chunked.
    if language is None or language in ("markdown", "json", "yaml", "toml"):
        return _line_window_chunks(
            file_path, content,
            language=language or "text",
            fhash=fhash,
        )

    chunkable = CHUNKABLE_NODE_TYPES.get(language)
    if not chunkable:
        # Language supported by tree-sitter but we don't have a chunkable
        # node-type set for it yet — fall back to line windows.
        return _line_window_chunks(file_path, content, language=language, fhash=fhash)

    parser = _try_tree_sitter(language)
    if parser is None:
        return _line_window_chunks(file_path, content, language=language, fhash=fhash)

    source_bytes = content.encode("utf-8", "ignore")
    try:
        tree = parser.parse(source_bytes)
    except Exception as e:
        log.debug("parse failed for %s (%s): %s", file_path, language, e)
        return _line_window_chunks(file_path, content, language=language, fhash=fhash)

    root = tree.root_node
    out: list[Chunk] = []
    # Line numbers of the gaps' start offsets, counted forward from the last
    # one asked (gaps come in file order), so a file of thousands of gaps is
    # read once.
    line_at = {"byte": 0, "line": 1}

    def _line_of(byte: int) -> int:
        if byte < line_at["byte"]:
            line_at.update(byte=0, line=1)
        line_at["line"] += source_bytes.count(b"\n", line_at["byte"], byte)
        line_at["byte"] = byte
        return line_at["line"]

    def _gap(start: int, end: int, kind: str) -> int | None:
        """The top-level code between definitions — module docstrings,
        constants, tables, assignments, a definition too small to be its own
        chunk — as chunks of `kind`. A gap shorter than MIN_CHUNK_CHARS that
        is not blank (one constant between two functions) is returned as the
        byte it starts at, for the definition after it to carry."""
        if start > 0 and source_bytes[start - 1:start] != b"\n":
            # The gap opens on the last line of the definition before it;
            # what is left of that line is blank or a trailing comment.
            nl = source_bytes.find(b"\n", start, end)
            rest_end = end if nl < 0 else nl + 1
            if not source_bytes[start:rest_end].strip():
                start = rest_end
        # Leading blank lines are not part of the code that follows them.
        while start < end:
            nl = source_bytes.find(b"\n", start, end)
            if nl < 0 or source_bytes[start:nl].strip():
                break
            start = nl + 1
        text = source_bytes[start:end].decode("utf-8", "ignore").rstrip()
        if len(text.strip()) < MIN_CHUNK_CHARS:
            return start if text.strip() else None
        out.extend(_line_window_chunks(
            file_path, text, language=language, fhash=fhash,
            first_line=_line_of(start), kind=kind, first_index=len(out)))
        return None

    pos = 0     # the first byte no chunk holds yet
    for node, raw_kind, symbol in _walk_top_level(root, source_bytes, chunkable):
        text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", "ignore")
        if len(text) < MIN_CHUNK_CHARS:
            continue    # stays in the gap around it
        lead = _gap(pos, node.start_byte, "module")
        kind = _kind_label(raw_kind)
        start_line = node.start_point[0] + 1
        if lead is not None and node.end_byte - lead <= MAX_CHUNK_CHARS:
            # The short gap before it rides in this definition's chunk.
            text = source_bytes[lead:node.end_byte].decode("utf-8", "ignore")
            start_line = _line_of(lead)
        elif lead is not None:
            out.extend(_line_window_chunks(
                file_path, source_bytes[lead:node.start_byte].decode("utf-8", "ignore").rstrip(),
                language=language, fhash=fhash, first_line=_line_of(lead),
                kind="module", first_index=len(out)))
        if len(text) <= MAX_CHUNK_CHARS:
            out.append(Chunk(
                file_path=file_path,
                chunk_index=len(out),
                content=text,
                language=language,
                chunk_kind=kind,
                symbol=symbol,
                start_line=start_line,
                end_line=node.end_point[0] + 1,
                file_hash=fhash,
            ))
        else:
            # Oversized logical unit — split it by lines and characters while
            # preserving the original kind/symbol metadata on every sub-chunk.
            out.extend(_line_window_chunks(
                file_path, text, language=language, fhash=fhash,
                first_line=node.start_point[0] + 1, kind=kind, symbol=symbol,
                first_index=len(out)))
        pos = max(pos, node.end_byte)

    # The code after the last definition (e.g. trailing top-level code Python
    # sees as `expression_statement`s after all defs).
    lead = _gap(pos, len(source_bytes), "file_tail")
    if lead is not None and out:
        # A short tail (`if __name__ == "__main__": main()`) has no definition
        # after it to ride in; it is a chunk of its own.
        out.extend(_line_window_chunks(
            file_path, source_bytes[lead:].decode("utf-8", "ignore").rstrip(),
            language=language, fhash=fhash, first_line=_line_of(lead),
            kind="file_tail", first_index=len(out)))

    if not out:
        # Pure-comment / pure-blank file with no chunkable AST nodes
        # → still record one line-window so search can hit it.
        return _line_window_chunks(file_path, content, language=language, fhash=fhash)

    return out
