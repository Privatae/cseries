"""The App SDK build contract.

Injected into the coding harness so generated/ported apps are compliant BY
CONSTRUCTION — a valid manifest, real scopes, SDK-native storage, no isolation
violations. The scope catalog is pulled LIVE from scopes.PERMISSIONS so the prompt
can never drift from what the build/publish validators enforce. Today the harness
gets only the static-deploy contract and no schema, which is why ~90% of generated
manifests are invalid.
"""
from __future__ import annotations

import re
from pathlib import Path


# Static-source scan signals. These catch the isolation/storage
# contract violations that the build prompt forbids but an LLM still emits — clear
# true-positives only, so a legitimate app is never falsely blocked.
#   - localStorage/sessionStorage: reset to empty when the app is isolated to its own
#     origin (the contract mandates CSeriesApp.storage, the only durable per-app store).
#   - window.parent / window.top / parent.* / top.*: cross the iframe boundary the
#     isolation model relies on (the injected SDK owns the only parent channel).
_RE_WEBSTORAGE = re.compile(r"\b(?:local|session)Storage\s*[.\[]")
_RE_PARENT_REACH = re.compile(
    r"\b(?:window\s*\.\s*(?:parent|top)|(?:parent|top)\s*\.\s*"
    r"(?:document|location|postMessage|frames|window))\b"
)
# fetch/XHR/WebSocket/EventSource to an ABSOLUTE external host — the exfil channel the
# per-app CSP connect-src pins to declared network[] hosts. THIS build check is the
# enforcement every app gets: the runtime CSP header is emitted only for published
# (community) apps (proxy.py _serve_static), so for anything else an undeclared host
# is caught here or not at all.
_RE_NET_CALL = re.compile(
    r"""(?:fetch|EventSource|WebSocket)\s*\(\s*['"`]\s*(https?://([^/'"`\s]+))"""
    r"""|\.open\s*\(\s*['"`][A-Z]+['"`]\s*,\s*['"`]\s*(https?://([^/'"`\s]+))""",
    re.IGNORECASE,
)
_SCAN_SUFFIXES = (".html", ".htm", ".js", ".mjs")
_SCAN_SKIP_DIRS = {"node_modules", "dist", "build", ".git", "bin"}
# Not a cap: every file is scanned. This is how much of one file is held in
# memory at a time; a bigger file is read in windows of this size.
_SCAN_WINDOW_BYTES = 2_000_000


def _scan_app_sources(project_dir: Path, declared_hosts: list[str],
                      exclude_top=frozenset()) -> list[str]:
    """Scan the built HTML/JS for runtime-contract violations the manifest schema
    can't see. Conservative (clear violations only). Returns violation strings."""
    declared = {h.strip().lower() for h in (declared_hosts or []) if h and str(h).strip()}
    # Every file, whatever its size or count (tree_walk.scan_windows): a big
    # file is read in windows, and a match counts in the window it starts in.
    # Findings are gathered per file first, so a file read in several windows
    # still reports each violation once.
    found: dict[str, dict] = {}
    # Through the contained walk: a link out of the project is never read, so
    # a violation message can never quote one.
    from server.applications.tree_walk import scan_windows

    def _hit(rx, text: str, lo: int, hi: int) -> bool:
        return any(lo <= m.start() < hi for m in rx.finditer(text))
    for rel, text, lo, hi in scan_windows(project_dir, suffixes=_SCAN_SUFFIXES,
                                          skip_dirs=_SCAN_SKIP_DIRS, exclude_top=exclude_top,
                                          window_bytes=_SCAN_WINDOW_BYTES):
        f = found.setdefault(rel, {"storage": False, "parent": False, "hosts": set()})
        f["storage"] = f["storage"] or _hit(_RE_WEBSTORAGE, text, lo, hi)
        f["parent"] = f["parent"] or _hit(_RE_PARENT_REACH, text, lo, hi)
        f["hosts"] |= {
            (m.group(2) or m.group(4) or "").lower()
            for m in _RE_NET_CALL.finditer(text)
            if lo <= m.start() < hi and (m.group(2) or m.group(4))
            and (m.group(2) or m.group(4)).lower() not in declared
        }
    errs: list[str] = []
    for rel, f in found.items():
        if f["storage"]:
            errs.append(
                f"{rel}: uses localStorage/sessionStorage — these RESET when the app is "
                f"isolated to its own origin; persist state with CSeriesApp.storage instead"
            )
        if f["parent"]:
            errs.append(
                f"{rel}: reaches window.parent/window.top — an isolated app must never "
                f"touch the parent frame/DOM (the injected SDK owns that channel)"
            )
        for host in sorted(f["hosts"]):
            errs.append(
                f"{rel}: calls undeclared external host {host!r} — add it to manifest "
                f"network[] (for a published app the per-app CSP blocks any host that "
                f"isn't listed there, and network[] is how the user sees where the app "
                f"talks to)"
            )
    return errs


def _unknown_manifest_keys(project_dir: Path) -> list[str]:
    """Violations for any top-level app.yaml key that isn't in the manifest schema.

    manifest.load() reads the keys it knows and IGNORES the rest, so an invented key
    (`retention_days: 30`, `author: …`) used to sail through silently and the app
    shipped believing a setting was in force that nothing ever read. This is the
    admission-time half of that: the app is being written right now, so the author can
    fix it. (load() stays tolerant — it also runs over already-installed apps, where
    raising would make a working app vanish from the dock; see manifest.py.)

    Reads the raw YAML, so it sees what the author wrote rather than what the loader
    kept. Silent when there's no readable app.yaml at the root — a missing/unparseable
    manifest is reported by the loader, not invented as a second error here."""
    try:
        import yaml as _yaml
        raw = _yaml.safe_load((project_dir / "app.yaml").read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    from server.applications.manifest import DECLARABLE_KEYS, unknown_keys
    bad = unknown_keys(raw)
    if not bad:
        return []
    return [
        f"app.yaml declares unknown field(s) {', '.join(repr(k) for k in bad)} — the "
        f"manifest schema is finite and nothing reads a field outside it. Remove them, "
        f"or use the right name: {', '.join(sorted(DECLARABLE_KEYS))}"
    ]


def _injected_keys_in_source(project_dir: Path) -> list[str]:
    """Violations for a SOURCE app.yaml that carries a value only the platform
    may write at pack time (manifest.PLATFORM_INJECTED_NESTED_KEYS: the packed
    commit, its tree hash, the lockfile's hash). Those values mean "the
    platform verified this"; an author writing one by hand would be claiming
    a verification that never happened. The loader accepts them because it
    also reads packed bundles — the refusal belongs here, at the source tree."""
    try:
        import yaml as _yaml
        raw = _yaml.safe_load((project_dir / "app.yaml").read_text(encoding="utf-8"))
    except Exception:
        return []
    from server.applications.manifest import injected_nested_keys
    bad = injected_nested_keys(raw)
    if not bad:
        return []
    return [
        f"app.yaml sets {', '.join(bad)} — the platform stamps these at pack time "
        f"from the repo and the packed bytes; remove them from the source manifest"
    ]


def validate_build(project_dir: Path, manifest, app=None, exclude_top=frozenset()) -> list[str]:
    """The personal/build light-bar: catch the failures the prompt
    alone can't guarantee, with clear messages back to the Workbench. The scope catalog
    and every field's type are already enforced by manifest.load(); this adds the
    unknown-FIELD check (which load() deliberately leaves to admission time), the
    static-deploy contract checks, and a conservative static-source scan for the
    isolation/storage/network violations an LLM still emits. Returns a list of
    violation strings ([] = ok).

    When `app` (the FastAPI app) is provided, ALSO runs the app-facing /api
    conformance gate (server/applications/api_contract.py): every own-origin
    /api call in the app is diffed against the live route contract — a call to a
    nonexistent route, or a scope-gated route the manifest doesn't declare, is a
    violation. Skipped when `app` is None (no app context — e.g. the offline tool)."""
    errors: list[str] = []
    ep = (manifest.entrypoint or "").strip()
    if not ep:
        errors.append("manifest entrypoint is empty")
        return errors
    # No build step → the entry point is a real file at the worktree root.
    if "/" in ep.strip("/"):
        errors.append(
            f"entrypoint {ep!r} must be a file at the worktree root, not a subfolder "
            f"(there is no build step; never dist/)"
        )
    if not (project_dir / ep).is_file():
        errors.append(
            f"entrypoint {ep!r} does not exist in the project — write {ep} at the root"
        )
    errors.extend(_unknown_manifest_keys(project_dir))
    errors.extend(_injected_keys_in_source(project_dir))
    # A declared lockfile must exist in the tree: the pack step hashes it and
    # the runtime pod installs from it, so a dangling declaration would ship
    # an app whose first boot refuses itself.
    _lock = getattr(getattr(manifest, "build", None), "lockfile", None)
    if _lock and not (project_dir / _lock).is_file():
        errors.append(
            f"build.lockfile {_lock!r} does not exist in the project — generate it "
            f"(pip-compile --generate-hashes -o {_lock} requirements.txt) and commit it"
        )
    # A process app may declare no hosts (it reaches nothing outside the
    # platform), but one that installs dependencies at first start reaches the
    # package index: with no hosts its first boot fails, so say so now. What
    # counts is what the file asks pip for, not that it exists: a
    # standard-library app's empty requirements.txt, or the lockfile that pins
    # nothing, installs nothing. A declared lockfile that is missing counts
    # (refused above, and here it cannot be shown to be empty).
    _rt = getattr(manifest, "runtime", None)
    if getattr(_rt, "kind", None) == "process" and getattr(_rt, "image", None):
        _net = list(getattr(manifest, "network", None) or [])
        _deps = None
        if _lock and _file_names_requirements(project_dir / _lock):
            _deps = _lock
        elif ((project_dir / "requirements.txt").is_file()
              and _file_names_requirements(project_dir / "requirements.txt")):
            _deps = "requirements.txt"
        if _deps and not _net:
            errors.append(
                "network[] is empty but the app installs dependencies at first start "
                f"({_deps}): list the package index's hosts "
                "(pypi.org, files.pythonhosted.org), or use only the standard library "
                "and leave network[] empty")
        elif _deps:
            try:
                _lines = _requirement_lines((project_dir / _deps).read_bytes())
            except OSError:
                _lines = []
            _missing = _index_hosts_missing(_net, _lines)
            if _missing:
                errors.append(
                    f"the app installs dependencies at first start ({_deps}) from the "
                    f"package index, and its pod reaches only the hosts network[] "
                    f"declares: add {', '.join(_missing)}")
    # exclude_top: top-level folders that are NOT this app — a project's
    # module directories when the root app is the one being built/shipped.
    errors.extend(_scan_app_sources(project_dir, getattr(manifest, "network", []) or [],
                                    exclude_top))
    if app is not None:
        from server.applications import api_contract
        errors.extend(api_contract.check_conformance(project_dir, manifest, app, exclude_top))
    return errors


# Default-DENY allowlist: the ONLY scopes an untrusted (community/third-party) app may
# hold. Anything not listed here is refused at publish — including any scope ADDED to the
# catalog later (a new privileged scope is denied by construction, never auto-granted).
# The set is deliberately minimal: scopes bound to the app's OWN data/UX, benign
# read-only system info, and the metered AI capabilities. Everything that reaches the host
# filesystem/network/processes, the platform (policy/backup/publish/incidents/security),
# other identities (circles/inbox/address_book), the user's private memory/brain, or their
# finances (billing) is OUT — those apps must ship first-party (standard/premium tier,
# which skips this bar). This is the tunable policy line; widen it per app-category demand.
COMMUNITY_ALLOWED_SCOPES = {
    # the app's own productivity data
    "read:calendar", "write:calendar",
    "read:tickets", "write:tickets",
    "read:projects", "write:projects",
    "read:decision", "write:decision", "resolve:decision",
    "read:review", "write:review",
    # the app's own session / sandbox / appearance
    "read:session", "write:session",
    "read:workspace_files", "write:workspace_files",
    "write:appearance",
    # benign read-only system info (no host/platform reach)
    "read:system_status", "read:version", "read:inference_catalog",
    # AI capabilities (the user opted in by installing)
    "execute:inference", "execute:websearch",
    # the peer app lane:
    # the same app on two hosts talking directly. Reach is bounded by the USER
    # twice — the peer must be an accepted contact, and the peer's host must
    # hold the same app with the same scope — so a store app can only ever
    # talk to its own copies on hosts its installer already trusts.
    "read:peer_app", "write:peer_app",
    # groups:
    # a roster and a signed vote. Admitted for a community app on the same
    # reasoning as the peer app scopes — the roster is the USER's, every
    # mutation requires this host to own the group, and an invite still needs
    # the invitee to be an accepted contact. "Start a group, build an app, grow
    # a following" only works if a store app may hold these.
    "read:group", "write:group",
    # inbox DELIVERY:
    # an app hands the host something it selected for the user — a mail routing
    # rule's match, a watcher's hit. Admitted on the same reasoning as the peer
    # app and group scopes: reach is bounded and the mutation is the user's own.
    # The row is a self-send on the installer's own host, the kind is pinned to
    # 'app_delivery', and the delivering app is stamped from the window token,
    # so every delivery is attributable and revocable by uninstalling.
    #
    # write:inbox is NOT here and must not be: it carries /send, which takes an
    # arbitrary from_address and message_kind and could therefore forge a
    # platform notification or a contract proposal.
    "write:inbox_delivery",
    # using a data-source connection the host granted this app. Same
    # reasoning again: it is inert by itself. The broker refuses every call
    # until the user grants THIS app THAT connection, the grant names one
    # account, and revoking it is a single act independent of uninstalling.
    # A store mail client can therefore ask for the user's Gmail and get it
    # only if the user says so, per account.
    #
    # write:portal_sources is NOT here and must not be: it creates and revokes
    # connections and holds the OAuth client, which is the host's business.
    "use:integration",
}

# There is NO platform ceiling on a store app's declared memory budget.
# The budget is
# declared by the author, shown at consent (store_preview), and charged to the
# INSTALLER's plan by the wake gate (_refuse_if_over_plan / AppRuntimeOverPlan
# rollback at install). A 2 GiB app is a 2 GiB line on the installer's pool,
# not a platform decision. The earlier 128Mi COMMUNITY_HELPER_CEILING_MI was a
# proposed number that was never ratified; it is gone.


def _att_filename() -> str:
    from server.applications.attestation import FILENAME
    return FILENAME


def _looks_like_source_zip(raw: bytes) -> bool:
    """A zip with an app.yaml (at the root or one level down) and no
    manifest.yaml — a source directory someone zipped, not a bundle."""
    import io as _io
    import zipfile as _zipfile
    try:
        with _zipfile.ZipFile(_io.BytesIO(raw)) as zf:
            names = [n.replace("\\", "/") for n in zf.namelist()]
    except Exception:
        return False
    if "manifest.yaml" in names:
        return False
    return any(n == "app.yaml" or (n.count("/") == 1 and n.endswith("/app.yaml"))
               for n in names)


def _unhashed_requirements(lock_bytes: bytes) -> list[str]:
    """Requirement entries in a pip lockfile that carry no --hash=. Logical
    lines are joined across `\\` continuations (pip-compile writes
    `pkg==1.0 \\` then `    --hash=sha256:…` lines); comments, blanks and
    option lines (`-r`, `--index-url`, …) are not requirements and are
    skipped. Every remaining entry must name at least one hash — the same
    rule `pip install --require-hashes` applies, checked at admission so the
    refusal names the entry instead of failing at first boot."""
    bad: list[str] = []
    for s in _requirement_lines(lock_bytes):
        if s.startswith("-"):
            continue
        if "--hash=" not in s:
            bad.append(s.split()[0][:60])
    return bad


def _requirement_lines(req_bytes: bytes) -> list[str]:
    """The logical lines of a pip requirements file that ask pip for
    anything: `\\` continuations joined, blanks and comments dropped. Option
    lines are kept (`-r more.txt` pulls more requirements in)."""
    text = req_bytes.decode("utf-8", "replace")
    logical: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        logical.append(buf)
        buf = ""
    if buf:
        logical.append(buf)
    return [s for s in (e.strip() for e in logical) if s and not s.startswith("#")]


# pip's default index, and the host its packages download from. A runtime
# pod installs a lockfile at first start (sidecars/app-runtime-python/run.py)
# through the app's egress wall, which lets a name out only when network[]
# declares it or a parent of it (sidecars/app-egress/egress.py _host_allowed).
# The app's container has no proxy of its own, so a
# lockfile app that does not declare these cannot install.
_PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org")
# Requirement-file options that REPLACE PyPI. Their hosts cannot be read off
# here, so a file carrying one is not judged. --extra-index-url and
# -f/--find-links are not among them: pip consults those IN ADDITION to the
# default index, so PyPI's hosts are still needed.
_INDEX_OPTIONS = ("--index-url", "--no-index")


def _index_hosts_missing(declared: list[str], req_lines: list[str]) -> list[str]:
    """The PyPI hosts a requirements file installs from that `declared`
    (network[]) does not let out, by the wall's own rule: an exact name, or a
    subdomain of a declared one. [] when the file replaces PyPI with its own
    index (-i/--index-url, in either spelling, or --no-index)."""
    for line in req_lines:
        opt = line.split()[0].split("=", 1)[0]
        if opt in _INDEX_OPTIONS or (opt.startswith("-i") and not opt.startswith("--")):
            return []
    hosts = [str(h).strip().lower().rstrip(".") for h in declared or [] if str(h).strip()]
    return [p for p in _PYPI_HOSTS
            if not any(p == h or p.endswith("." + h) for h in hosts)]


def _file_names_requirements(path: Path) -> bool:
    """Does this requirements file ask pip for anything? A file that cannot be
    read counts as yes: the check it feeds refuses, and a refusal naming the
    file is the safe answer."""
    try:
        return bool(_requirement_lines(path.read_bytes()))
    except OSError:
        return True


def validate_publish(bundle_bytes: bytes) -> list[str]:
    """Strict admission bar for an app headed to the store.
    Store apps run on other people's hosts, so they: may hold ONLY the default-deny
    allowlist of app-appropriate scopes (COMMUNITY_ALLOWED_SCOPES — anything else, incl.
    any future-added scope, is refused), and must be SDK-bound (validated_under_sdk) so
    they were judged by a known SDK. Provenance is NOT judged here — the operator
    issues it (rewrite-to-community + community-key signature) after this bar; see
    the comment below. Returns a list of violation strings ([] = ok). The runtime
    walls (own pod + egress wall + per-app CSP exfil wall + token scopes) are the
    containment; this is admission.

    A store app may also be a
    PROCESS app, provided it is a RECIPE the installer's machine can reproduce
    and contain: a platform toolchain image (never a custom binary), a declared
    wall, a declared budget, and a hashed lockfile packed in the bundle. What
    is distributed is that recipe; the runtime pod assembles it from the
    lockfile and nothing else."""
    from server.applications import bundles
    import yaml as _yaml
    try:
        contents = bundles._read_zip(bundle_bytes)
        m = _yaml.safe_load(contents.manifest_yaml) or {}
    except Exception as e:
        # A zip that is a SOURCE tree (app.yaml + files, no manifest.yaml /
        # payload/ layout) is what a hand-assembled upload looks like. Name
        # the real reason — it carries no platform build record — instead of
        # the layout error, so the Publisher's status line says what to do.
        if _looks_like_source_zip(bundle_bytes):
            return [f"hand-assembled zip: no platform build record "
                    f"({_att_filename()}) — the store accepts only bundles the "
                    f"platform packed from a committed project; publish from the "
                    f"Workbench (your host packs and submits it)"]
        return [f"unreadable bundle/manifest: {e}"]

    # Provenance is deliberately NOT checked here. It is a TRUST axis issued by
    # who SIGNS (installer anchor: manifest.provenance must be in
    # signer.provenance_allowed — a community key can only sign `community`),
    # and on the publish path the ISSUER is the operator: publish_zip
    # force-rewrites `provenance: community` into the manifest and re-runs this
    # bar AFTER the rewrite, before signing with the community key
    # (controller features/appstore/service.py). An earlier version refused any
    # non-community self-declaration here, which deadlocked the platform's own
    # Workbench lane: _pack_and_sign_project requires provenance=custom, so the
    # kernel-side pre-check refused every bundle the platform itself produced,
    # and nothing could ever publish. Checking the SUBSTANCE (scopes + SDK
    # binding) on whatever is declared is strictly stronger than refusing on
    # the label — a self-declared "standard" bundle no longer skips the bar,
    # it gets held to it, and the operator stamps community regardless.
    errors: list[str] = []
    from server.applications.manifest import (
        ALLOWED_RUNTIME_IMAGES, DECLARABLE_KEYS, unknown_keys,
    )
    # Runtime admission. A
    # store app is static, a platform helper, or a process app that runs in
    # its OWN pod rendered from a platform toolchain image. `external` (a
    # custom OCI sidecar) has no home here: the toolchain is always the
    # platform's, the author ships source. Every pod-backed kind must declare
    # its wall (network[]) and its budget (baseline_memory_mi) — the two
    # numbers the consent screen and the wake gate run on. No ceiling on the
    # budget: it is the installer's plan that pays, and the consent screen
    # says so.
    rt_raw = (m.get("runtime") or {}) if isinstance(m.get("runtime") or {}, dict) else {}
    rt_kind = str(rt_raw.get("kind") or "static")
    rt_image = str(rt_raw.get("image") or "").strip().lower() or None
    net = [str(h).strip() for h in (m.get("network") or []) if str(h).strip()]
    res_raw = (m.get("resources") or {}) if isinstance(m.get("resources") or {}, dict) else {}
    bm = res_raw.get("baseline_memory_mi")
    bld_raw = (m.get("build") or {}) if isinstance(m.get("build") or {}, dict) else {}
    lock_rel = str(bld_raw.get("lockfile") or "").strip() or None
    lock_sha = str(bld_raw.get("lockfile_sha256") or "").strip().lower() or None

    if rt_kind not in ("static", "helper", "process"):
        errors.append(
            f"store apps may declare runtime=static, runtime=helper or "
            f"runtime=process (with a platform toolchain image), not {rt_kind!r} "
            f"— a custom sidecar image is not a recipe the installer can reproduce")
    if rt_kind == "process":
        if rt_image is None:
            errors.append(
                "runtime=process in the store requires runtime.image (one of "
                f"{', '.join(sorted(ALLOWED_RUNTIME_IMAGES))}) — the process runs in "
                "its own pod built from a platform toolchain, never beside the kernel")
        elif rt_image not in ALLOWED_RUNTIME_IMAGES:
            errors.append(
                f"runtime.image {rt_image!r} is not a platform toolchain "
                f"(allowed: {', '.join(sorted(ALLOWED_RUNTIME_IMAGES))})")
        # The pinned half of the recipe. A process app that installs whatever
        # PyPI serves on first boot is the one input the installer could not
        # verify; the lockfile with hashes is how it becomes verifiable, and
        # the runtime pod installs from it with --require-hashes.
        if lock_rel is None:
            errors.append(
                "runtime=process in the store requires build.lockfile — a hashed "
                "lockfile packed with the app, which the runtime pod installs from "
                "and nothing else (pip-compile --generate-hashes produces one)")
        else:
            lock_bytes = contents.payload.get(lock_rel)
            if lock_bytes is None:
                errors.append(
                    f"build.lockfile {lock_rel!r} is declared but not packed in the "
                    f"bundle — it must ship with the app")
            else:
                import hashlib as _hashlib
                actual = _hashlib.sha256(lock_bytes).hexdigest()
                if lock_sha is None:
                    errors.append(
                        "build.lockfile_sha256 is missing — the pack step stamps it "
                        "from the packed bytes, and the bar checks that the recipe "
                        "and the bytes agree (the proof that the PLATFORM packed "
                        "them is attestation.json)")
                elif lock_sha != actual:
                    errors.append(
                        f"build.lockfile_sha256 does not match the packed "
                        f"{lock_rel!r} (declared {lock_sha[:12]}…, packed "
                        f"{actual[:12]}…) — the recipe and the bytes disagree")
                unhashed = _unhashed_requirements(lock_bytes)
                if unhashed:
                    errors.append(
                        f"build.lockfile {lock_rel!r} has requirement(s) without a "
                        f"--hash= ({', '.join(unhashed[:5])}"
                        f"{', …' if len(unhashed) > 5 else ''}) — every entry must "
                        f"pin bytes, not names; the runtime pod installs with "
                        f"--require-hashes and refuses the file otherwise")
    if rt_kind in ("helper", "process"):
        # A helper fetches from its hosts, so it must name them. A process app
        # may name none: an empty network[] is a pod that reaches nothing
        # outside the platform (manifest.load says why). But one that installs
        # its dependencies at first start reaches the package index, so a
        # declared lockfile that pins anything, with no hosts, is an app whose
        # first boot fails. The store requires a lockfile of every process app,
        # so one that pins nothing (a standard-library app) is how an app that
        # reaches nothing is published; pip installs nothing from it. A
        # declared lockfile missing from the bundle counts (refused above).
        if rt_kind == "helper" and not net:
            errors.append(
                f"runtime={rt_kind} requires a non-empty network[] — the declared "
                f"hosts ARE the pod's wall")
        _lock_payload = contents.payload.get(lock_rel) if lock_rel else None
        if (rt_kind == "process" and not net and lock_rel
                and (_lock_payload is None or _requirement_lines(_lock_payload))):
            errors.append(
                f"network[] is empty but build.lockfile {lock_rel!r} is declared: the "
                f"process installs its dependencies at first start, from the package "
                f"index, so list its hosts (pypi.org, files.pythonhosted.org)")
        elif rt_kind == "process" and net and _lock_payload is not None:
            _missing = _index_hosts_missing(net, _requirement_lines(_lock_payload))
            if _missing and _requirement_lines(_lock_payload):
                errors.append(
                    f"build.lockfile {lock_rel!r} pins packages the process installs at "
                    f"first start from the package index, and its pod reaches only the "
                    f"hosts network[] declares: add {', '.join(_missing)}")
        if not isinstance(bm, int) or isinstance(bm, bool) or bm <= 0:
            errors.append(
                f"runtime={rt_kind} requires resources.baseline_memory_mi (the "
                f"pod's declared budget — shown at consent and charged to the "
                f"installer's plan)")
    # The platform's build record (attestation.py): the bundle must
    # carry attestation.json agreeing with its manifest and its bytes, and
    # describing a committed, clean tree. This is what makes the store accept
    # only what the platform packed from a repo — a hand-assembled zip, or an
    # edit that never entered git, has no record and cannot publish.
    from server.applications import attestation as _att
    errors.extend(_att.verify_bundle(m, contents.payload, contents.binaries))
    # Anything that is not a process app has no build: the loader refuses
    # build.lockfile without runtime.image (manifest.py load), and a helper can
    # never carry an image — so a helper bundle with a lockfile that passed
    # here would be signed by the store and refused by every installer.
    # Mirror the loader's rule exactly.
    if rt_kind != "process" and (lock_rel is not None or lock_sha is not None):
        errors.append(f"build.lockfile applies to a runtime.image (process) app; "
                      f"a {rt_kind} app has no dependencies to pin")
    bad_keys = unknown_keys(m)
    if bad_keys:
        errors.append(
            f"manifest declares unknown field(s) "
            f"{', '.join(repr(k) for k in bad_keys)} — the schema is finite and nothing "
            f"reads a field outside it; declarable fields: "
            f"{', '.join(sorted(DECLARABLE_KEYS))}"
        )
    perms = [str(p) for p in (m.get("permissions") or [])]
    bad = sorted(p for p in perms if p not in COMMUNITY_ALLOWED_SCOPES)
    if bad:
        errors.append(
            "community apps may only request the app-appropriate scope allowlist; "
            f"not permitted: {', '.join(bad)}"
        )
    if not m.get("validated_under_sdk"):
        errors.append(
            "missing validated_under_sdk — build the app through the SDK so it is bound "
            "to a verified App SDK version"
        )
    # The app-data contract. A store app runs on other people's hosts and its
    # rows are those people's rows, so the ownership key is admission, not
    # style: it is what lets a person take their own rows with them and nobody
    # else's. load() already raises on this for the build path; here the whole
    # list is reported at once, because the Publisher shows one status line.
    from server.applications.manifest import data_contract_violations
    errors.extend(f"app-data contract: {e}" for e in data_contract_violations(m))
    # Live context cards read that same declared data, so they are admitted
    # against it here, all at once, for the same one status line.
    from server.applications.manifest import live_context_cards
    errors.extend(f"live context: {e}" for e in live_context_cards(m)[1])
    # An MCP server for the app's own Host: only from the app's own pod.
    from server.applications.manifest import mcp_spec
    errors.extend(f"mcp: {e}" for e in mcp_spec(m)[1])
    return errors


# ── The ONE statement of how a `process` backend lives today ──────────────
# Both audiences read this exact sentence: the coding harness inside
# app_sdk_build_contract() and the host inside host_runtime_brief(). It is a
# constant, not prose in two places, because the two places once
# disagreed — default-skills/build-workflow.md told the host `process` was for
# "something that must run when no window is open" while supervisor.py
# terminates the sidecar on the last window close (Supervisor: "First window
# open spawns. Last window close terminates."). The host planned a 24x7
# trading loop against the false sentence, concluded the pod could not host
# it, and recommended a VPS. scripts/test_host_runtime_brief.py asserts the
# skill no longer carries its own version of this fact.
#
# When the resident lifecycle ships (a manifest `runtime.lifecycle: resident`
# that survives window close — the decided direction), this
# sentence changes HERE and both readers change with it.
def _runtime_images() -> frozenset:
    """The toolchain families a process app may name — read from the manifest
    module so this text can never advertise a family the loader refuses."""
    try:
        from server.applications.manifest import ALLOWED_RUNTIME_IMAGES
        return ALLOWED_RUNTIME_IMAGES
    except Exception:
        return frozenset({"python"})


PROCESS_LIFECYCLE_TODAY = (
    "ONE process serves every open window of the app; by default "
    "(runtime.lifecycle: window) it is killed when the last one closes, and with "
    "runtime.lifecycle: resident it keeps running with no window open — started "
    "at boot, restarted on a crash, switched on/off by the user (or on a "
    "schedule) — until they stop it"
)


def host_runtime_brief() -> str:
    """What THIS machine can run, in the user's language — for the host at
    ALIGNMENT time, not the harness at build time.

    The real problem was the host getting too technical in the alignment
    phase. The harness gets the full SDK law
    (app_sdk_build_contract, prepended to every build prompt). The host was
    getting nothing at plan time and filled the gap with architecture it
    inferred — including that a personal daemon needed an external server.
    This block is the accurate, non-technical answer to "can my machine run
    this?", derived from the same constants the validator enforces
    (manifest.ALLOWED_RUNTIME_KINDS, PROCESS_LIFECYCLE_TODAY), so it can
    never say a kind exists that the manifest refuses, or the reverse.
    """
    try:
        from server.applications.manifest import ALLOWED_RUNTIME_KINDS
        kinds = set(ALLOWED_RUNTIME_KINDS)
    except Exception:
        kinds = {"static", "process", "external", "helper"}

    lines = [
        "## What this machine can run",
        "The user's apps run on the user's own machine — this pod. That is "
        "settled; where an app runs is not a planning question. Never propose "
        "a VPS, a bastion, a cloud box or any other server as part of a plan. "
        "If this machine genuinely cannot do something today, name exactly "
        "what it cannot do, in one sentence, and `escalate` — it is a platform "
        "question, not a hosting question.",
        "",
        "Stay at the user's altitude while aligning: what the app does, what "
        "it watches, when it should be working, what the user wants to see. "
        "Do not design runtime internals here — lifecycles, proxies, tokens, "
        "sidecar memory. The coding engine receives the full build contract "
        "when the build starts, and the platform validates what it produces. "
        "Your job at alignment is to get the WANT right, not the HOW.",
        "",
        "The kinds of app this machine runs, in plain terms:",
    ]
    if "static" in kinds:
        lines.append(
            "- A **static** app: pages the machine serves and the browser runs. "
            "Zero resident cost. The default, and right for most things — "
            "dashboards, viewers, forms, anything over an existing API."
        )
    if "process" in kinds:
        lines.append(
            "- A **process** app: a backend program running on this machine "
            "behind the app, in its own container built from a real toolchain "
            f"({', '.join(sorted(_runtime_images()))}: interpreter, package "
            "installer, git, a C compiler), with a data directory that survives "
            f"restarts. {PROCESS_LIFECYCLE_TODAY}. So a bot, a poller or a "
            "trading loop that must run around the clock is a resident process "
            "app on THIS machine — the user decides when it runs (always, or a "
            "start/stop schedule) and sees its memory in the machine panel. "
            "That is the answer to \"I need this running 24/7\"; another server "
            "never is."
        )
    others = sorted(kinds - {"static", "process"})
    if others:
        # Deliberately ONE non-technical line. These kinds exist for store
        # apps and platform sidecars; a personal build never plans around
        # them, and describing them invites exactly the architecture talk
        # this block forbids.
        lines.append(
            "- Other kinds (" + ", ".join(f"**{k}**" for k in others) + ") "
            "exist for store apps and platform-shipped sidecars — nothing you "
            "plan for here."
        )
    lines += [
        "",
        "Work that must happen on a schedule with no window open — a check "
        "every hour, a report each morning — is a scheduled task "
        "(`schedule_task`), which runs with nobody present and needs no "
        "resident process at all.",
        "",
        "## Delivery is proven, not announced",
        "Install is not delivery. After every install of a project — the "
        "user's 'Install as my app', your `ship_project_app`, or the plan's "
        "own end with auto-deliver on — the platform starts the app and PROVES "
        "it runs: its own ready check answering ok, the last lines it printed "
        "clean, twice in a row. The verdict arrives in the project thread as "
        "a 'prove: <app>' task, and the delivered turn is written from that "
        "envelope. So: say 'installed, being proven' until it lands; say "
        "'delivered' only when it says delivered; never tell the user to open "
        "a live app on the strength of a build that landed files.",
        "A proof that fails carries the app's own log, its ready body and its "
        "exit code, plus a ready-made repair brief (the seed field). With auto-deliver "
        "on, the platform dispatches that repair itself — one seeded follow-up "
        "build, up to two rounds — re-ships and re-proves; you only report. "
        "With auto-deliver off, or once the rounds are spent, YOU dispatch it: "
        "`build(action='continue', message=<the seed>)` — the seed is the whole "
        "brief, do not paraphrase it, do not add a plan. `system.app_status` "
        "shows the same log and exit for any app at any time, pod-backed ones "
        "included; read it before saying an app is up or diagnosing one.",
    ]
    return "\n".join(lines)


def app_sdk_build_contract() -> str:
    try:
        from server.applications.scopes import PERMISSIONS
        scopes = ", ".join(sorted(PERMISSIONS))
    except Exception:
        scopes = "(fetch GET /api/applications/scopes)"
    publish_allow = ", ".join(sorted(COMMUNITY_ALLOWED_SCOPES))

    # The fields the contract spells out one by one. Everything else the schema allows is
    # listed from the schema itself, so this prompt can never claim a field is
    # unknown while the validator accepts it (or the reverse) — the two are the
    # same set by construction.
    _spelled_out = {
        "id", "name", "entrypoint", "provenance", "version", "permissions",
        "network", "csp_strict_inline", "window", "storage", "runtime",
        "license", "source", "build",
    }
    try:
        from server.applications.manifest import DECLARABLE_KEYS
        rest = ", ".join(sorted(DECLARABLE_KEYS - _spelled_out))
    except Exception:
        rest = "(fetch GET /api/applications/api-contract)"

    return (
        "APP SDK CONTRACT (AUTHORITATIVE — the app is validated against this before it is "
        "signed/installed; non-compliant output is rejected):\n"
        "\n"
        "1. MANIFEST — write `app.yaml` at the worktree root:\n"
        "   - id: stable kebab-case slug (REQUIRED)\n"
        "   - name: human title (REQUIRED)\n"
        "   - entrypoint: index.html (REQUIRED — must match the actual file)\n"
        "   - provenance: custom (REQUIRED for Workbench-built apps)\n"
        "   - version: semver, e.g. 0.1.0\n"
        "   - permissions: [...] — ONLY values from the catalog in (2); request the "
        "MINIMUM the app needs; no wildcards. NOTE: an app intended for PUBLISH "
        "(community tier) may hold ONLY this allowlist — anything else is refused at "
        f"publish: {publish_allow}.\n"
        "   - network: [host, ...] — EVERY external host the app calls (e.g. "
        "api.example.com). The build REJECTS a call to a host that isn't listed, and "
        "for a published app the browser blocks it too (the per-app CSP pins "
        "connect-src to this list). Omit if the app only calls its own /api.\n"
        "   - csp_strict_inline: true (optional, published/community apps) — hardens "
        "script-src to your own inline-script hashes and DROPS 'unsafe-inline', so an "
        "injected script can't run. Requires NO inline event handlers (use "
        "addEventListener, not onclick=\"…\") and NO javascript: URLs.\n"
        "   - license: {publish_site, redistribute, flavors, source_visible, notes} "
        "(optional) — YOUR terms for whoever installs this app, and the only place "
        "they are set. Every right DEFAULTS TO FALSE, so saying nothing withholds "
        "it: publish_site=true lets an installer put a WEBSITE built from your app "
        "on the public web (refused otherwise, and that refusal is enforced, not "
        "advisory); redistribute=true lets them republish it to the store under "
        "their own name (refused at publish otherwise); flavors: open|closed "
        "decides whether anyone may publish a FLAVOUR — a child of your app, with "
        "your authorship still recorded on the parent; source_visible=true lets the "
        "store listing link to your platform repo (installers always have the "
        "source on their own machine — this governs only the public link). notes "
        "is your own message to the installer. None of it binds YOU: your own app "
        "is always yours to publish however you like. Ask the user what they want "
        "here rather than choosing for them — it is a rights decision, not a "
        "default.\n"
        "   - source: {repo, upstream: {url, ref, license}} (optional) — where the "
        "code is from. repo is the project's platform repo (owner/name); upstream "
        "names the open-source origin when the app is built on or ported from one, "
        "with its licence as an SPDX id (REQUIRED whenever url is set — the store "
        "shows it and the publish gate reads it). NEVER write source.commit or "
        "source.tree: the platform stamps them at pack time, and a source app.yaml "
        "carrying them is refused.\n"
        "   - build: {lockfile} (runtime.image apps) — the path of a HASHED lockfile "
        "the runtime pod installs from with --require-hashes BEFORE your command "
        "runs (into a venv at /var/lib/cseries/appdata/<id>/venv that is first on "
        "PATH, so `python`/`pip` in your command are that venv's). Generate it in "
        "the build: `pip install pip-tools && pip-compile --generate-hashes "
        "--output-file requirements.lock requirements.txt` and COMMIT "
        "requirements.lock. REQUIRED for a process app headed to the store; with "
        "it declared, your bootstrap must NOT pip-install anything else (an "
        "unhashed install is refused). NEVER write build.lockfile_sha256: the "
        "platform stamps it at pack time.\n"
        "   - window: {title, width, height} (optional)\n"
        "   - storage: {workspace: true} (optional — only for a large filesystem "
        "workspace; the KV store in (3) is always available)\n"
        "   - runtime: {kind: process, image: python, command: [...]} (optional) — a "
        "BACKEND. By default an app is static HTML/JS in the browser sandbox and can "
        "only call /api routes. Declare this when the app must RUN CODE: `image` "
        f"names the toolchain family ({', '.join(sorted(_runtime_images()))}) and is "
        "REQUIRED for a custom app — the process runs in the app's OWN runtime pod "
        "built from that toolchain (python 3.14, pip, git, a C compiler and the "
        "usual build headers are present; the pod's user is uid 1000 with no root "
        "and no apt; `creative` is that same toolchain plus `blender` (5.2.2, run "
        "headless with `blender -b`), `photocraft-cli` and `filmcraft-cli` on PATH, "
        "and no GPU), with the install dir as its working directory and "
        "/var/lib/cseries/appdata/<id>/ as its persistent data dir. `command` runs "
        "with these env vars — PORT (or whatever you set as port_env), "
        "PRIVATAE_APP_ID, CSERIES_KERNEL_URL, and APP_RUNTIME_SOURCE_TOKEN, the app's own "
        "token (an app that declares `mcp: {path: /mcp}` serves MCP to its Host there "
        "and must refuse any request whose `Authorization: Bearer` is not that "
        "token); it listens on every interface "
        "(0.0.0.0 / ::) — the readiness probe hits the POD IP, so a "
        "loopback-only bind never becomes Ready. ready_path (default /health) is polled until it "
        "answers, then /system-apps/<id>/ is reverse-proxied to it. READY MEANS READY: "
        "while the app is still installing, bootstrapping or broken, ready_path answers "
        "{\"ok\": false, \"phase\": \"<what it is doing>\"} or {\"ok\": false, \"error\": "
        "\"<what failed>\"} (HTTP 200 is fine — the pod probe stays green), and {\"ok\": "
        "true} ONLY once it can actually serve. The platform reads that body, not the "
        "status code: a stub that answers 200 over a dead app is a contract violation, "
        "and it is caught, because after every install the platform STARTS the app "
        "and PROVES it — polls ready_path until ok, reads the last lines the process "
        "printed (stdout+stderr are captured for pod apps too), and only then reports "
        "it delivered; an app that never answers ok, exits, or keeps printing "
        "error/traceback lines is reported NOT delivered with that log as the repair "
        "brief. Print what you are doing and what failed, one line each. An image "
        "app must also declare resources.baseline_memory_mi (the pod's memory "
        "limit), may declare resources.vcpus (1-8, the pod's CPU ceiling, billed as "
        "it runs; unset is 1; a renderer or encoder that uses every core finishes "
        "sooner at more) and declares network[] (the hosts the process may reach; [] when it reaches "
        "none, e.g. a game that keeps its own state, which is allowed and "
        "walls the pod shut; an app that pip-installs at first start lists the "
        "package index's hosts, pypi.org and files.pythonhosted.org, beside its "
        f"own: the pod has no other way out). {PROCESS_LIFECYCLE_TODAY}. "
        "Install dependencies into the appdata dir at first start (a venv there "
        "survives sleeps); nothing is installed into the image. THE APPDATA DIR "
        "PERSISTS ACROSS RE-INSTALLS: a re-ship replaces the install dir, never "
        "appdata, so anything the app derives from its shipped files — a config "
        "it copies, an exchange or endpoint it writes down, a data dir named after "
        "a setting — must be re-applied from the shipped files on EVERY boot (keep "
        "generated secrets and databases; refresh the settings). A first-boot-only "
        "copy means the user's next change never reaches the running app. There is NO token in the "
        "environment: the caller's `X-Application-Token` arrives as a header on "
        "each proxied request, and your process re-sends THAT header on its own "
        "calls back to CSERIES_KERNEL_URL — so it acts with the caller's scopes for "
        "that one request and holds no standing credential of its own "
        "(cli/cseries_sidecar.py is this pattern, ready to import). A backend is the "
        "ONLY way to reach something on the pod's loopback that is not an /api "
        "route — the browser sidecar's control API on port 8830, for example. A "
        "PUBLISHED (community) app may declare runtime=static, runtime=helper "
        "(the platform's own fetch+cache runtime in its own scale-to-zero pod, "
        "walled to the app's declared network[] hosts), or runtime=process WITH "
        "runtime.image — its own pod from a platform toolchain, walled to "
        "network[], budgeted by resources.baseline_memory_mi (no ceiling: the "
        "installer's plan pays and the consent screen says so), assembled from "
        "build.lockfile and nothing else. Never runtime=external: a custom sidecar "
        "image is not a recipe the installer can reproduce, so install refuses it.\n"
        f"   - also allowed, all optional: {rest}\n"
        "   That list is the WHOLE manifest. Nothing reads a field outside it, so "
        "inventing one is not a harmless extra — the build and the publish gate both "
        "REJECT an app.yaml with an unknown field, naming it.\n"
        "\n"
        f"2. SCOPE CATALOG (the only valid `permissions` values):\n   {scopes}\n"
        "\n"
        "3. RUNTIME — the platform INJECTS `window.CSeriesApp` into your app's HTML "
        "and hands it the window's token (do NOT add app-sdk.js yourself, do NOT "
        "hardcode a token):\n"
        "   - `await CSeriesApp.ready` before the first /api call if you need the token "
        "immediately. The token arrives from the shell just after load, so `ready` is "
        "the only thing you ever wait for.\n"
        "   - Call your own origin's `/api/...` with RELATIVE urls and send no "
        "credential: the SDK stamps `X-Application-Token` onto every fetch for you.\n"
        "   - Persist state with `await CSeriesApp.storage.set(key, value)` / `.get(key)` "
        "/ `.remove(key)` / `.keys()`. Those are a thin wrapper over your own per-app "
        "config store — PUT/GET/DELETE `/api/applications/<your id>/config/<key>`, GET "
        "`/api/applications/<your id>/config` for all of it — which needs no scope "
        "(the token binds it to your app id; another app's token is refused) and "
        "outlives an uninstall unless the user purges the app. Do NOT use "
        "localStorage/sessionStorage: they are per-origin, they are wiped if the app "
        "is ever served on its own isolated origin, and the build rejects them.\n"
        "   - If your app keeps ROWS THAT BELONG TO A PERSON (not just its own "
        "settings), declare them: `data: {tables: [{name: <table>, columns: [...]}]}`. "
        "Every declared table MUST carry the owner-subject column — `owner_subject` "
        "unless you set `data.subject_column` — and that column MUST appear in that "
        "table's `columns` list, or the build rejects the app naming the table. This "
        "is what lets a person who used your app on a public site take THEIR rows, "
        "and only theirs, into their own Host: the move selects on that column. "
        "Without it the only honest move is the whole table, which is everyone "
        "else's rows too. An app that keeps no per-person rows omits `data` entirely.\n"
        "   - To FIND things — the user's memory, their uploaded documents, their "
        "projects, the journal, their code, the Host's skills — call ONE route: POST /api/search with "
        "a JSON body {q, limit, stores} (scope read:search). The words go in the "
        "body, never in the URL: a URL is kept in logs, and a search box sends what "
        "is typed one key at a time. It runs the same "
        "recall the host runs before a turn, plus the other stores, each under its "
        "own key with an honest `unavailable` reason when it could not run. Do not "
        "stitch the five per-store search routes together yourself.\n"
        "   - The app-facing API surface is CODE-TRUE and FINITE: GET "
        "/api/applications/api-contract lists every /api route you may call, each with its "
        "required scope and (where the route declares a model) its request/response keys. "
        "Call ONLY routes listed there; declare each route's scope in permissions[]; read "
        "ONLY the response keys it lists. Do NOT invent a route, a param, or a response key. "
        "The build REJECTS an app that calls a route with no server handler, or a "
        "scope-gated route whose scope the manifest didn't declare.\n"
        "\n"
        "4. ISOLATION RULES (hard — a published app that violates these is refused):\n"
        "   - NEVER touch window.parent, window.top, or the parent's DOM/cookies.\n"
        "   - NEVER call an external host not declared in `network`.\n"
        "\n"
        "5. EXAMPLE app.yaml:\n"
        "   id: notes\n"
        "   name: Notes\n"
        "   entrypoint: index.html\n"
        "   provenance: custom\n"
        "   version: 0.1.0\n"
        "   permissions: []\n"
        "   window: {title: Notes, width: 600, height: 700}\n"
        "   # in index.html: await CSeriesApp.ready; "
        "const draft = await CSeriesApp.storage.get('draft');\n"
    )
