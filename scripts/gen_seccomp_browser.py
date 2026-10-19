"""Derive seccomp-browser.json from seccomp-personal.json + a documented delta.

Kept as the single definition of the difference so the two profiles cannot
drift: tests/test_seccomp_browser_profile.py re-runs this and asserts equality.
"""
import json, sys, pathlib

BASE = pathlib.Path("seccomp-personal.json")
OUT  = pathlib.Path("seccomp-browser.json")

# Namespace bits masked out of clone by the base profile (0x7E020000).
_NS_MASK = 2114060416

def derive(base: dict) -> dict:
    p = json.loads(json.dumps(base))   # deep copy
    p["_comment"] = (
        "CEIGAS browser sidecar — hardened seccomp profile. seccomp-personal.json "
        "plus the syscalls Chromium's own layer-1 sandbox needs to build a user + "
        "mount + pid namespace for its renderers. Derived, never hand-edited: see "
        "scripts/gen_seccomp_browser.py and tests/test_seccomp_browser_profile.py, "
        "which fails if this file and the base drift apart."
    )
    out = []
    for g in p["syscalls"]:
        names = list(g.get("names", []))
        # unshare: Chromium needs it to create its sandbox namespaces.
        # setns STAYS blocked — entering an EXISTING namespace is the escape
        # primitive; creating your own is the sandbox.
        if g.get("action") == "SCMP_ACT_ERRNO" and set(names) == {"unshare", "setns"}:
            out.append({"_comment": "BLOCKED: setns — joining an existing namespace "
                                    "is the escape primitive and Chromium never does it.",
                        "names": ["setns"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1})
            out.append({"_comment": "ALLOWED for the browser: unshare — Chromium's "
                                    "sandbox creates its own user/mount/pid namespaces. "
                                    "Confined to those namespaces; cannot touch the host.",
                        "names": ["unshare"], "action": "SCMP_ACT_ALLOW"})
            continue
        # mount family: Chromium pivot_roots into an empty dir inside its OWN
        # mount namespace. Unprivileged, and invisible outside that namespace.
        if g.get("action") == "SCMP_ACT_ERRNO" and "pivot_root" in names:
            out.append({"_comment": "ALLOWED for the browser: mount family — used only "
                                    "inside the sandbox's own mount namespace to build "
                                    "an empty root. readOnlyRootFilesystem, drop-ALL caps "
                                    "and no-new-privs all still apply to the container.",
                        "names": sorted(names), "action": "SCMP_ACT_ALLOW"})
            continue
        # clone: drop the no-new-namespaces mask.
        if g.get("action") == "SCMP_ACT_ALLOW" and names == ["clone"]:
            args = g.get("args") or []
            assert args and args[0].get("value") == _NS_MASK, (
                f"base clone rule changed shape ({args}) — re-read it before regenerating")
            out.append({"_comment": "ALLOWED for the browser: clone WITH CLONE_NEW* — the "
                                    "base profile masks these off (adv-bench R1). Chromium's "
                                    "sandbox is built out of exactly those flags, so masking "
                                    "them leaves the container with NO layer-1 sandbox at all "
                                    "while it renders hostile web content. Chosen 2026-08-24 "
                                    "over passing --no-sandbox.",
                        "names": ["clone"], "action": "SCMP_ACT_ALLOW"})
            continue
        out.append(g)
    p["syscalls"] = out
    return p

if __name__ == "__main__":
    base = json.loads(BASE.read_text())
    OUT.write_text(json.dumps(derive(base), indent=2) + "\n")
    print(f"wrote {OUT}")
