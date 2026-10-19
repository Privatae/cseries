"""seccomp-browser.json must stay seccomp-personal.json plus a known delta.

Two profiles that are 95% the same rot apart silently: someone hardens the
base, nobody regenerates the derivative, and the browser container keeps
running last quarter's floor while everything else moved. These tests make
that a red build.

The delta exists because of a real conflict, worth stating where it will be
read. The base profile masks CLONE_NEW* off `clone` and ERRNOs `unshare`
(the comment in it says "adv-bench R1: block namespace creation that
bypasses blocked unshare/setns"). Chromium's own layer-1 sandbox is built
out of exactly those flags. Under the base profile Chromium cannot build a
sandbox, and since the container also runs with no-new-privs — which
neuters the SUID helper — it has no sandbox at all and fails to launch.

The two ways out were `--no-sandbox`, or letting this one container create
its own namespaces. The latter was chosen on 2026-08-24: Chromium's
sandbox is the thing actually containing hostile web content, and removing
it to satisfy an outer profile trades a strong specific boundary for a
weaker general one.

What is deliberately NOT widened: `setns` (joining an existing namespace is
the escape primitive; creating your own is the sandbox), `ptrace`,
`userfaultfd`, `personality`, `memfd_create`, and the module/reboot family.

Run: pytest tests/test_seccomp_browser_profile.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BASE = _ROOT / "seccomp-personal.json"
_BROWSER = _ROOT / "seccomp-browser.json"
_GEN = _ROOT / "scripts" / "gen_seccomp_browser.py"


def _load(p: pathlib.Path) -> dict:
    assert p.exists(), f"{p.name} missing"
    return json.loads(p.read_text())


def _derive():
    spec = importlib.util.spec_from_file_location("gen_seccomp_browser", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.derive(_load(_BASE))


def _rule_for(profile: dict, syscall: str) -> dict:
    hits = [g for g in profile["syscalls"] if syscall in g.get("names", [])]
    assert len(hits) == 1, f"{syscall} appears in {len(hits)} groups, expected 1"
    return hits[0]


# ── the drift guard ─────────────────────────────────────────────────


def test_browser_profile_is_exactly_the_base_plus_the_documented_delta():
    """If this fails, someone changed one profile and not the other.
    Re-run scripts/gen_seccomp_browser.py and read the diff."""
    assert _load(_BROWSER) == _derive(), (
        "seccomp-browser.json is not what gen_seccomp_browser.py produces from "
        "seccomp-personal.json — regenerate it, do not hand-edit")


def test_the_generator_notices_if_the_base_clone_rule_changes_shape():
    """The generator asserts on the base's clone mask rather than blindly
    replacing the rule — so a base change lands as a loud failure here
    instead of a silently wrong derivative."""
    import copy
    base = copy.deepcopy(_load(_BASE))
    for g in base["syscalls"]:
        if g.get("names") == ["clone"] and g.get("args"):
            g["args"][0]["value"] = 12345
    spec = importlib.util.spec_from_file_location("gen2", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(AssertionError, match="clone rule changed shape"):
        mod.derive(base)


# ── what the delta must be ──────────────────────────────────────────


def test_chromium_can_create_its_sandbox_namespaces():
    p = _load(_BROWSER)
    assert _rule_for(p, "unshare")["action"] == "SCMP_ACT_ALLOW"
    clone = _rule_for(p, "clone")
    assert clone["action"] == "SCMP_ACT_ALLOW"
    assert not clone.get("args"), (
        "clone still carries the no-new-namespaces mask — Chromium's sandbox "
        "is built from CLONE_NEWUSER/NEWPID/NEWNS and cannot start")


def test_mount_family_is_allowed_for_the_sandbox_root():
    p = _load(_BROWSER)
    for call in ("mount", "umount2", "pivot_root"):
        assert _rule_for(p, call)["action"] == "SCMP_ACT_ALLOW", call


def test_clone3_still_returns_enosys_so_glibc_falls_back():
    """clone3 takes a struct, so its flags cannot be seccomp-filtered.
    ENOSYS (38) — not EPERM — is what makes glibc retry via clone, which
    IS filterable. EPERM here would break pthread_create outright."""
    r = _rule_for(_load(_BROWSER), "clone3")
    assert r["action"] == "SCMP_ACT_ERRNO" and r["errnoRet"] == 38


# ── what must NOT have been widened ─────────────────────────────────


def test_setns_stays_blocked():
    """Creating your own namespace is the sandbox. Joining someone
    else's is the escape."""
    assert _rule_for(_load(_BROWSER), "setns")["action"] == "SCMP_ACT_ERRNO"


@pytest.mark.parametrize("call", [
    "ptrace", "userfaultfd", "personality", "memfd_create",
    "init_module", "finit_module", "kexec_load", "reboot",
])
def test_the_rest_of_the_hardening_is_untouched(call):
    base_action = _rule_for(_load(_BASE), call)["action"]
    assert _rule_for(_load(_BROWSER), call)["action"] == base_action == "SCMP_ACT_ERRNO"


def test_default_action_is_still_deny():
    p = _load(_BROWSER)
    assert p["defaultAction"] == "SCMP_ACT_ERRNO"
    assert p.get("defaultErrnoRet") == _load(_BASE).get("defaultErrnoRet")


def test_the_allowlist_was_not_quietly_expanded():
    """Only the four rules above may differ. Anything else added to the
    343-call allowlist is a widening nobody reviewed."""
    base_allowed = {n for g in _load(_BASE)["syscalls"]
                    if g["action"] == "SCMP_ACT_ALLOW" for n in g["names"]}
    browser_allowed = {n for g in _load(_BROWSER)["syscalls"]
                       if g["action"] == "SCMP_ACT_ALLOW" for n in g["names"]}
    assert browser_allowed - base_allowed == {
        "unshare", "mount", "umount", "umount2", "pivot_root"}
    assert base_allowed - browser_allowed == set()
