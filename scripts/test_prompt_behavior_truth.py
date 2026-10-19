#!/usr/bin/env python3
"""
test_prompt_behavior_truth.py — anti-staleness gate for app prompt_behavior.

The rule: the direction we put in front of a host must be
rooted in truth and must NEVER go stale. The brain's old static prompt content
rotted precisely because nothing checked it against the live platform — it was
written before C-Series existed and drifted silently.

This gate enforces ONE hard invariant:

    Every backticked `identifier` in any app's prompt_behavior
    (what_it_is / user_path / step_up) MUST resolve to
    something real — a tool the host can actually call
    (PUBLIC_TOOL_SCHEMAS ∪ META_TOOL_CAPS) or a real installed app id.

A renamed, removed, typo'd, or fabricated reference (e.g. an `open_app` tool
that does not exist) fails the build instead of silently misleading the host
into promising a capability it does not have.

Authoring convention this enforces: in prompt_behavior, backtick ONLY real tool
names or app ids. Plain prose words do not get backticks.

Run inside the image (the brain deps are present there):
    python3 scripts/test_prompt_behavior_truth.py
Exits non-zero on any unresolved reference. Wired into smoke_test.sh.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _apps_dir() -> Path:
    """Resolve the system-apps directory in both the host source tree
    (<repo>/system-apps) and inside the image (the kernel
    mounts/bakes them at /app/system/apps)."""
    for cand in (ROOT / "system-apps", Path("/app/system/apps"),
                 Path("/app/system-apps"), ROOT / "system" / "apps"):
        if cand.exists():
            return cand
    return ROOT / "system-apps"


APPS_DIR = _apps_dir()
BACKTICK = re.compile(r"`([^`]+)`")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.\-]+")


def _real_references():
    """The set of things prompt_behavior is allowed to backtick: every tool the
    host can actually call, plus every real app id. Imported from the live
    registry so this gate tracks reality, not a hardcoded list."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "brain"))

    tool_names: set[str] = set()
    try:
        from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
        for schema in PUBLIC_TOOL_SCHEMAS.values():
            name = (schema.get("function") or {}).get("name")
            if name:
                tool_names.add(name)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  cannot import PUBLIC_TOOL_SCHEMAS ({e}) — "
              f"run this gate inside the cseries image", file=sys.stderr)
        sys.exit(2)

    try:
        from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
        tool_names |= set(META_TOOL_CAPS.keys())
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  cannot import META_TOOL_CAPS ({e})", file=sys.stderr)
        sys.exit(2)

    from server.applications.manifest import load_all
    apps = load_all(APPS_DIR)
    app_ids = {m.id for m in apps}
    return tool_names, app_ids, apps


def main() -> None:
    tool_names, app_ids, apps = _real_references()
    known = tool_names | app_ids

    failures: list[tuple[str, str]] = []
    checked = 0
    for m in apps:
        pb = m.prompt_behavior
        fields = [pb.what_it_is or "", pb.user_path or "", pb.step_up or ""]
        for text in fields:
            for tok in BACKTICK.findall(text):
                tok = tok.strip()
                # Only identifier-shaped tokens are treated as tool/app
                # references; a backticked prose phrase (spaces, slashes,
                # punctuation) is not a reference and is ignored.
                if not IDENTIFIER.fullmatch(tok):
                    continue
                checked += 1
                if tok not in known:
                    failures.append((m.id, tok))

    # This repo's own manifests carry one paragraph and no coaching list.
    # The parser loads a published manifest that
    # still has either; the apps shipped from here are held to the rule.
    import yaml
    from server.applications.manifest import WHAT_IT_IS_MAX
    shape: list[str] = []
    for y in sorted(APPS_DIR.glob("*/app.yaml")):
        pb_raw = (yaml.safe_load(y.read_text()) or {}).get("prompt_behavior") or {}
        if "do_when_active" in pb_raw:
            shape.append(f"{y.parent.name}: prompt_behavior.do_when_active (how to work in an app is a skill)")
        what = str(pb_raw.get("what_it_is") or "").strip()
        if len(what) > WHAT_IT_IS_MAX:
            shape.append(f"{y.parent.name}: what_it_is is {len(what)} characters (at most {WHAT_IT_IS_MAX})")
    if shape:
        print("FAIL  prompt_behavior shape — this repo's apps carry one paragraph and no coaching list:",
              file=sys.stderr)
        for line in shape:
            print(f"        {line}", file=sys.stderr)
        sys.exit(1)

    if failures:
        print("FAIL  prompt_behavior truth gate — references that are NOT a real "
              "tool or app (stale, renamed, or fabricated):", file=sys.stderr)
        for app_id, tok in failures:
            print(f"        {app_id}: `{tok}`", file=sys.stderr)
        print("\n  A host reads these as things it can do. If the name is wrong, "
              "the host promises a capability it does not have.", file=sys.stderr)
        print(f"  Real tools: {sorted(tool_names)}", file=sys.stderr)
        print(f"  Real apps:  {sorted(app_ids)}", file=sys.stderr)
        sys.exit(1)

    print(f"OK    prompt_behavior truth gate — {checked} backticked references "
          f"across {len(apps)} apps all resolve to a real tool or app")


if __name__ == "__main__":
    main()
