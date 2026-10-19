"""mastercode.runtime.code_tree_page — one PAGE of a repo tree for the host.

The operator's /repo-tree walks a whole allowlisted repo (capped at 6,000
files) and returns it nested; the bridge op `code.tree` hands that back
unchanged. Under the 100 KB transcript wall a 6,000-file tree lands whole:
One `code_tree` result measured 76,502 chars
— the largest single item in a Host's window. A tree is for orientation; nobody
orients on 6,000 lines.

page_tree() turns the operator's nested tree into the page the host asked
for: a subtree (`path`), to a depth (`depth`), capped (`max_entries`), with
honest counts and the next narrower call spelled out. Pure; no I/O.
"""
from __future__ import annotations

DEFAULT_DEPTH = 2
DEFAULT_MAX_ENTRIES = 300
MAX_ENTRIES = 2000
MAX_DEPTH = 12


def _norm_path(path) -> str:
    return "/".join(p for p in str(path or "").replace("\\", "/").split("/") if p and p != ".")


def _find(node: dict, want: str) -> dict | None:
    if not want:
        return node
    if _norm_path(node.get("path")) == want:
        return node
    for child in node.get("children") or []:
        if child.get("type") != "directory":
            continue
        cp = _norm_path(child.get("path"))
        if cp == want:
            return child
        if want.startswith(cp + "/"):
            return _find(child, want)
    return None


def page_tree(data: dict, *, path: str = "", depth: int = DEFAULT_DEPTH,
              max_entries: int = DEFAULT_MAX_ENTRIES) -> dict:
    """One page of the tree. `data` is the operator's {repo, tree, total_files,
    truncated}. Returns {repo, path, depth, entries, shown, entries_total,
    total_files, source_truncated, truncated, note} — or {"error": ...} when
    the subtree does not exist."""
    try:
        depth = max(1, min(int(depth or DEFAULT_DEPTH), MAX_DEPTH))
    except (TypeError, ValueError):
        depth = DEFAULT_DEPTH
    try:
        max_entries = max(1, min(int(max_entries or DEFAULT_MAX_ENTRIES), MAX_ENTRIES))
    except (TypeError, ValueError):
        max_entries = DEFAULT_MAX_ENTRIES
    want = _norm_path(path)
    root = (data or {}).get("tree") or {}
    node = _find(root, want) if isinstance(root, dict) else None
    if node is None:
        return {"error": f"no directory '{want or '/'}' in the tree"
                         + (" (the operator's walk was capped, so it may exist beyond the cap)"
                            if (data or {}).get("truncated") else "")}

    entries: list[dict] = []
    total = 0

    def walk(n: dict, level: int) -> None:
        nonlocal total
        for child in n.get("children") or []:
            total += 1
            if len(entries) < max_entries:
                e = {"path": child.get("path"), "type": child.get("type")}
                if child.get("type") == "directory":
                    e["files"] = child.get("file_count")
                else:
                    e["size"] = child.get("size")
                entries.append(e)
            if child.get("type") == "directory" and level < depth:
                walk(child, level + 1)

    walk(node, 1)
    truncated = total > len(entries)
    note = (f"{len(entries)} of {total} entries under '{want or '/'}' to depth {depth}"
            + (f"; narrow with path='<subdir>' or raise max_entries (≤{MAX_ENTRIES})"
               if truncated else "")
            + (" — the operator's own walk hit its file cap, so deeper paths may be missing"
               if (data or {}).get("truncated") else ""))
    return {
        "repo": (data or {}).get("repo"),
        "path": want or "/",
        "depth": depth,
        "entries": entries,
        "shown": len(entries),
        "entries_total": total,
        "files_under_path": node.get("file_count"),
        "total_files": (data or {}).get("total_files"),
        "source_truncated": bool((data or {}).get("truncated")),
        "truncated": truncated,
        "note": note,
    }


def tree_from_entries(entries: list, *, repo=None, clipped: bool = False) -> dict:
    """The forge's flat tree (gitea op='tree': [{path, type, size}], type
    'blob' or 'tree') as the operator's nested shape, so page_tree pages a
    repo read from the forge the same way. A directory the list only
    implies (a prefix was asked for) is made from its files' paths.
    `clipped` (the forge's listing cap) is carried as `truncated`."""
    root = {"name": "", "path": "", "type": "directory", "children": []}
    dirs: dict[str, dict] = {"": root}

    def _dir(path: str) -> dict:
        node = dirs.get(path)
        if node is None:
            parent_path, _, name = path.rpartition("/")
            node = {"name": name, "path": path, "type": "directory", "children": []}
            _dir(parent_path)["children"].append(node)
            dirs[path] = node
        return node

    for e in entries or []:
        if not isinstance(e, dict):
            continue
        p = _norm_path(e.get("path"))
        if not p:
            continue
        if e.get("type") == "tree":
            _dir(p)
            continue
        parent_path, _, name = p.rpartition("/")
        _dir(parent_path)["children"].append(
            {"name": name, "path": p, "type": "file", "size": e.get("size") or 0})

    def _count(node: dict) -> int:
        n = 0
        for c in node["children"]:
            n += _count(c) if c["type"] == "directory" else 1
        node["file_count"] = n
        node["children"].sort(key=lambda c: (c["type"] != "directory", c["name"]))
        return n

    total = _count(root)
    return {"repo": repo, "tree": root, "total_files": total, "truncated": bool(clipped)}
