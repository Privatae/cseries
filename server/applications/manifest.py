"""
Manifest loader + validator for app.yaml files.

An app's app.yaml is the contract. It declares:
  id          — stable string the launcher and token bind to
  name        — display name
  entrypoint  — relative path inside the app's install dir; for
                runtime=static this is the HTML the dashboard loads,
                for runtime=process this is informational (the dashboard
                still loads "/system-apps/<id>/" — which is reverse-
                proxied to the sidecar).
  provenance  — the app's TRUST class: one of server/applications/
                kernel.py::ALLOWED_PROVENANCE. Defaults to standard when
                omitted; `premium` is coerced to standard (dead
                vocabulary). An app may only claim `native` if its id is
                listed in kernel.py::KERNEL_REQUIRED_APPS. `tier` is the
                permanently-accepted legacy spelling (signature-covered
                in already-published bundles).
  permissions — finite list from server/applications/scopes.py
  runtime     — optional. When present, declares a sidecar process:
                {kind: process, command: [...], port_env: PORT,
                 ready_path: /health, ready_timeout_seconds: 30}.
                Omit it (or use kind=static) for HTML-only apps.
  storage     — optional. {workspace: true} provisions a writable per-app
                directory at /var/lib/cseries/appdata/<id>/ that survives
                uninstall. Config storage (PG table) is always available
                and survives uninstall by default.
  window      — geometry hints for the launcher
  license     — the author's terms for an installer (LicenseSpec; default
                deny on every transferable right)
  source      — the recipe's origin: platform repo (+ platform-stamped
                commit/tree) and the upstream it came from, with its licence
  build       — the recipe's assembly: a hashed lockfile the runtime pod
                installs from (runtime.image apps only)

The loader rejects manifests with unknown permissions, an unknown
provenance, an illegitimate native claim, an invalid runtime block, or
missing required fields. This is the gate every app passes through
before it can be launched.

UNKNOWN KEYS are validated on WRITE, not on READ. `unknown_keys()` below
names every key that is not part of the schema, and the two ADMISSION
gates call it — build (build_contract.validate_build) and publish
(build_contract.validate_publish) — so an invented key is refused with an
actionable message before an app is signed or installed. `load()` itself
stays tolerant on purpose: it also runs at launcher time over apps that
are ALREADY installed, and load_all() skips a manifest it can't load — so
raising there would make a live app silently vanish from a user's dock on
an image upgrade, long after the moment they could have fixed it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from server.applications.kernel import (
    ALLOWED_PROVENANCE,
    KERNEL_REQUIRED_APPS,
    PROVENANCE_MANIFEST_KEYS,
)
from server.applications.scopes import validate as validate_permissions


REQUIRED_FIELDS = ("id", "name", "entrypoint")
DEFAULT_PROVENANCE = "standard"

# App id is used as a path segment (INSTALL_ROOT/<id>, /var/lib/cseries/appdata/<id>),
# a URL segment, and a token/registry key — so it must be a safe slug. An
# unvalidated id was a path-traversal vector (e.g. id "../../app/server").
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Ids no app may hold, because an app token carrying one would be read as a
# different caller. `host` is what kernel_ops mints when the HOST calls a
# route (kernel_ops.py, mint(..., app_id="host")) and routes that let the
# host act on behalf of a named app key that privilege on it
# (server/routes/peer_apps.py). `__shell__` is the desktop shell's token id
# (routes/applications.py SHELL_APP_ID); the regex already cannot match it,
# listed so the reservation is one set rather than two rules. `host` once
# matched _ID_RE, so installing an app by that name
# would have handed it the host's on-behalf-of branch.
RESERVED_APP_IDS: frozenset[str] = frozenset({"host", "__shell__"})


# `helper`: the app's server side is the
# PLATFORM's fetch+cache runtime in its own per-user, scale-to-zero pod,
# configured from this manifest's network[] + resources.baseline_memory_mi.
# The author ships no server code and declares no port/command — the operator
# assigns the port and provisions the pod from the install-time descriptor.
# The only community-legal runtime kind besides static (installer.py).
ALLOWED_RUNTIME_KINDS = frozenset({"static", "process", "external", "helper"})
# runtime.lifecycle (kind=process only) — see RuntimeSpec.lifecycle.
ALLOWED_RUNTIME_LIFECYCLES = frozenset({"window", "resident"})
# runtime.image (kind=process only): the TOOLCHAIN family the app's process
# needs — the platform maps each family to one digest-pinned image the app's
# own runtime pod is rendered from (the operator's APP_RUNTIME_IMAGES).
# Families, not arbitrary image refs: the user's app declares "I am a python
# program", never "run this container", so the code that runs beside their
# data is always a platform-built toolchain. Add a family here AND in the
# operator's pin table together.
#   python    sidecars/app-runtime-python
#   creative  sidecars/app-runtime-creative: the python
#             toolchain plus Blender, photocraft-cli and filmcraft-cli, pinned
#             by sha256, so a store app that drives them stays a python recipe.
ALLOWED_RUNTIME_IMAGES = frozenset({"python", "creative"})
# The first-party provenances: the ones only the C-Series key may sign
# (trust_store.seed_cseries_key). The name is older than the store lane.
# Only the ids in scripts/image_baked_apps.txt ship in the
# image; every other first-party app, claude-code included, installs from the
# store. A process app of these provenances MAY leave out `runtime.image` and
# run beside the kernel (supervisor.py; claude-code's harness is that case),
# or name an image and run in its own runtime pod like any other (the
# Telegram connector, system-apps/telegram). Custom (host-built) and community
# process apps are user code and MUST name an image (see load()).
IMAGE_BAKED_PROVENANCES = frozenset({"native", "standard"})
DEFAULT_READY_TIMEOUT = 30
# The memory REQUEST the operator gives an app's runtime pod, by form
# (the operator's _APP_RT_REQ_MI, rendered for both a helper and a process,
# with baseline_memory_mi as the pod's LIMIT). Kubernetes refuses a pod whose
# request is above its limit, so a smaller baseline_memory_mi installs and then
# never starts: refused here, at author time. Change it with the operator's.
APP_POD_REQUEST_MI = {"helper": 64, "process": 128}


@dataclass(frozen=True)
class WindowHints:
    title: str | None = None
    width: int | None = None
    height: int | None = None
    resizable: bool = True
    min_width: int | None = None
    min_height: int | None = None
    # Open filling the desktop (the shell's maximize state); user can restore.
    start_maximized: bool = False
    # Phone-mode rendering. "scale" (default): the shell renders the app at
    # its designed width and scales the frame down to fit a narrow screen —
    # the desktop layout shrinks instead of deforming. "responsive": the app
    # declares it reflows on narrow viewports itself; the shell renders it
    # native full-width. Declare responsive only after actually testing the
    # app at phone widths.
    mobile: str = "scale"


@dataclass(frozen=True)
class McpSpec:
    """An MCP server this app runs for its OWN Host.

    `mcp: {path: /mcp}` on a process app that runs in its own pod: the app
    serves MCP (Streamable HTTP, POST) at that path on its PORT, and the
    installer makes it a connection of the Host it is installed on
    (server/mcp_connections.sync_app), named after the app. Installing the app
    is the person's consent; nothing leaves the namespace, so no egress rule
    is drafted. The Host's existing rules still hold: it calls the tools only
    through operate, only after writing skills/mcp-<app id>, and their text
    comes back fenced as data.

    The endpoint must refuse any request without `Authorization: Bearer
    <APP_RUNTIME_SOURCE_TOKEN>`: the same per-app token the pod already holds
    (harness_backend.app_source_token), which the kernel presents. Other pods
    in the namespace can reach the app's port; only the kernel holds the token.

    path — "" (the default) means the app serves no MCP."""
    path: str = ""


_MCP_PATH_RE = re.compile(r"^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")


# An app's icon is a FILE in its own bundle: a relative path of plain name
# segments ending in an image extension. The shell fetches it as
# /system-apps/<id>/<icon> and the proxy answers exactly that path from disk
# (proxy._declared_icons), so the value must be the path the browser will send:
# no scheme, query, fragment, %-escape or dot segment that the browser or the
# server would rewrite into a path the app's backend answers instead.
_ICON_PATH_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){0,7}"
    r"\.(?:png|jpe?g|gif|webp|avif|svg|ico)", re.IGNORECASE)


def icon_path(value: Any) -> str | None:
    """The manifest's `icon` / `icon_small` if it is such a path, else None.
    None, not a refusal: an app with an odd icon still loads and shows its
    letter, as an app with no icon does."""
    if isinstance(value, str) and _ICON_PATH_RE.fullmatch(value):
        return value
    return None


def mcp_spec(raw: dict[str, Any]) -> tuple[McpSpec, list[str]]:
    """(the app's McpSpec, violations) for its `mcp:` block. Absent = none.
    Only a process app in its own pod can serve one: a static app has no
    server, and a process app beside the kernel is first-party."""
    blk = raw.get("mcp") if isinstance(raw, dict) else None
    if blk is None:
        return McpSpec(), []
    if not isinstance(blk, dict):
        return McpSpec(), ["mcp must be a mapping like {path: /mcp}"]
    errs = [f"mcp.{k}: not a key of mcp (only path)" for k in blk if k != "path"]
    path = blk.get("path")
    if not isinstance(path, str) or len(path) > 200 or not _MCP_PATH_RE.match(path) \
            or any(seg in (".", "..") for seg in path.split("/")):
        errs.append("mcp.path must be an absolute path like /mcp (letters, digits, . _ ~ -)")
    rt = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
    if rt.get("kind") != "process" or not rt.get("image"):
        errs.append("mcp is served by the app's own process: it needs runtime.kind: process "
                    "with a runtime.image")
    return (McpSpec(path=path) if not errs else McpSpec()), errs


# One screen of the cached prompt, measured: Circles' narration was 2,004
# characters of what_it_is plus 3,953 of coaching lines on every turn.
WHAT_IT_IS_MAX = 900


def clip_what_it_is(text: str) -> str:
    """`text` within WHAT_IT_IS_MAX characters, cut after the last full
    sentence that fits (or at the cap when no sentence ends inside it)."""
    if len(text) <= WHAT_IT_IS_MAX:
        return text
    head = text[:WHAT_IT_IS_MAX]
    end = max(head.rfind(". "), head.rfind(".\n"))
    return head[:end + 1].rstrip() if end > 0 else head.rstrip()


@dataclass(frozen=True)
class PromptBehavior:
    """How this app shows up in the entity's system prompt when active.

    Read at prompt-build time by runtime.prompt_builder._build_surface_role.
    Apps declare their own "what I am + how to act when you're in me"
    instead of the prompt builder hardcoding it. New apps wire their
    own block; the builder doesn't grow a new if/elif branch.

    what_it_is — one paragraph, at most WHAT_IT_IS_MAX characters: what the
      app IS and what lives in it, so the Host answers "where is X". No
      instructions: how to work in an app is a skill the Host reads on
      demand (default-skills/). `do_when_active`, the coaching list that
      once rode here, is ignored when a published manifest still has it.
    project_scoped — when true, the builder adds a hint that the chat
      history below is the prior turns on this specific project rather
      than ambient instructions. Used by the Workbench; circles is false.
    user_path — where a non-technical user finds this app, in plain words
      ("the Network app, from the dock"). The dashboard is a desktop OS
      with a dock/launcher, NOT a settings tree. The host uses this to
      navigate the user ("that's in the Network app — want me to open
      it?"), alongside the open_app tool which opens it directly.
    step_up — non-empty when actions in this app can WIDEN access and so
      need the user's elevated approval (Authentik TOTP step-up). A short
      note the host surfaces so it PROPOSES the change and guides the user
      to approve it — never just refuses. Empty = nothing here is gated.

    Absent → builder falls back to a generic body using the app's
    `description` field + a single "Treat this as general conversation
    unless the user tells you otherwise." line.
    """
    what_it_is: str = ""
    project_scoped: bool = False
    user_path: str = ""
    step_up: str = ""


@dataclass(frozen=True)
class StorageSpec:
    """Per-app survivable storage hints.

    config       — always true at the API layer; PG-backed key/value
                   store. Each app sees only its own namespace.
    workspace    — when true, the kernel provisions
                   /var/lib/cseries/appdata/<id>/ at install time and
                   leaves it intact through uninstall (unless ?purge=true).
    """
    workspace: bool = False


@dataclass(frozen=True)
class TableSpec:
    """One table a data-bearing app keeps its rows in.

    `columns` is the whole column list, and `subject_column` must be one of
    them. That pairing is the point: a table that names a subject column it
    does not have would declare an ownership key nothing can read, and the
    export side would select on a column that isn't there.
    """
    name: str
    columns: tuple[str, ...] = ()
    subject_column: str = ""


@dataclass(frozen=True)
class DataSpec:
    """The app's data contract: which tables hold its rows, and which column in
    each one names the Authentik subject that owns the row.

    An app with no `data` block is NOT data-bearing under this contract. Its
    only persistence is the kernel's per-app key/value store (app_config,
    migrations/v030_app_runtime_tables.sql), which is keyed by (app_id, key)
    and holds no per-person rows.

    WHY THE SUBJECT IS DECLARED PER TABLE RATHER THAN ASSUMED. The rows a
    person creates on a public site have to be findable by that person alone
    when they move to their own Host. A keyed selection needs a column name;
    a dump does not, and a dump is exactly what must not happen here, because
    it would carry every other member's rows with it. Declaring the key is
    what makes the move keyed.

    `subject_column` at this level is the default for every table; a table may
    override it. Both are checked against the table's own `columns`.
    """
    subject_column: str = "owner_subject"
    tables: tuple[TableSpec, ...] = ()

    @property
    def bearing(self) -> bool:
        """True when this app declares rows it owns on behalf of people."""
        return bool(self.tables)


@dataclass(frozen=True)
class RuntimeSpec:
    """How to launch this app.

    kind=static     The dashboard loads /system-apps/<id>/<entrypoint>
                    directly from the install dir's payload/. The
                    kernel never spawns a process.

    kind=process    `command` runs with exactly three env vars injected —
                    <port_env> (default PORT), PRIVATAE_APP_ID and
                    CSERIES_KERNEL_URL — ready_path is polled until it
                    answers, then /system-apps/<id>/ is reverse-proxied to
                    that port. ONE process per app, shared by every open
                    window of it, torn down when the last one closes
                    (lifecycle=window) or kept up (lifecycle=resident).

                    WHERE it runs is decided by `image` (a Python app whose first boot
                    needed apt + a C toolchain + python 3.11 could run
                    NOWHERE — the entity container is uid 1000 with no
                    compiler and only the kernel's python, and the only
                    other runtime was a scratch Go helper):
                      * image set  → the app's OWN runtime pod, rendered
                        from the platform's toolchain image for that
                        family, on the app-runtime wake/release lane
                        (harness_backend.ensure_awake_app). Its appdata
                        dir is a per-app PVC that survives sleeps.
                      * image unset → allowed ONLY for the first-party
                        provenances (IMAGE_BAKED_PROVENANCES, signed by
                        the Privatae key): the process runs beside the
                        kernel (supervisor.py). A custom/community process
                        app without `image` is refused at load.
                    `runs_in_pod(manifest)` is the one predicate every
                    caller uses; nothing else decides placement.

                    There is NO app token in the environment. The sidecar
                    authenticates per REQUEST: the browser's window token
                    arrives on the proxied call as X-Application-Token
                    (proxy.py forwards it — it is not a hop-by-hop
                    header), and the sidecar re-presents that same header
                    on its own calls back to CSERIES_KERNEL_URL. So its
                    reach is exactly the caller's scopes, per request,
                    and it holds no standing credential of its own.
                    cli/cseries_sidecar.py::KernelClient is that pattern.

    kind=external   The runtime is a k8s pod-level sidecar container
                    (separate OCI image) already listening on a fixed
                    loopback port. The kernel doesn't spawn anything;
                    proxy.py reverse-proxies /system-apps/<id>/* +
                    WebSocket upgrades to localhost:<port>. Used by
                    the browser sidecar and future harness
                    OCI images. Install signing gates which apps can
                    claim this kind.
    """
    kind: str = "static"
    # kind=process only. "window" (default): the supervisor spawns the sidecar
    # on the first open window and terminates it when the last one closes.
    # "resident": the sidecar is started at kernel boot (and on demand) and
    # SURVIVES the last window close — a daemon, a poller, a trading loop. The
    # user switches it on/off (installed_applications.resident_enabled) and can
    # schedule that switch with schedule_task; a crash is auto-restarted with a
    # bounded backoff and surfaced to the inbox like any sidecar exit. A
    # personal always-on process lives in the pod, never on a
    # VPS. A distributed app can be resident too: the store
    # bar (build_contract.validate_publish) admits a process app that names a
    # runtime.image, and the first-party Telegram connector (system-apps/
    # telegram) is a resident process app in its own pod.
    lifecycle: str = "window"
    # kind=process only: the toolchain family (ALLOWED_RUNTIME_IMAGES) of the
    # runtime pod this app runs in. None = a first-party app that runs beside
    # the kernel. See the class docstring.
    image: str | None = None
    command: list[str] = field(default_factory=list)
    port_env: str = "PORT"
    ready_path: str = "/health"
    ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT
    # Required for kind=external; ignored otherwise. The proxy forwards
    # to localhost:port (shared pod netns).
    port: int | None = None
    # kind=external only. The harness id backing this app on the scale-to-zero
    # pool (system-apps/browser: `harness: browser`).
    # None = an in-pod sidecar. Read here so INSTALL can provision it: once
    # only proxy._read_harness read this key, straight from the
    # yaml, and install provisioned a harness only for the hardcoded editor id
    # — so the Browser app installed from the store had no Deployment behind
    # it and every open died with "no harness-browser Deployment in
    # entity-<id> — is 'browser' in Entity.spec.harnesses?".
    harness: str | None = None
    # kind=external only. True (default): the /system-apps/<id>/ prefix is
    # stripped before forwarding, so the sidecar serves at root (the Browser
    # sidecar). False: the full /system-apps/<id>/<path> is forwarded unchanged —
    # required by sidecars that emit ABSOLUTE asset URLs under the public path
    # (a server run with a base path of /system-apps/<id>/…).
    strip_prefix: bool = True
    # kind=helper only: per-host cache TTL overrides in seconds
    # ({host: seconds}); hosts absent here get the helper's default. Rides
    # the install-time descriptor into the operator's ConfigMap — the
    # weather app's weekly-cadence sources (USDM, ONI) are the origin case.
    ttls: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ResourcesSpec:
    """What running this app costs the user's machine — DECLARED, not enforced.

    baseline_memory_mi — the memory (MiB) the app's runtime claims while it is
    running. This is the App Store's cost surface: the store renders it so a
    user sees what an app draws from the plan's machine memory BEFORE choosing
    it. Enforcement lives elsewhere and must agree with it:
      - external-runtime apps that wake on the harness pool are gated by the
        operator's per-spec `budget_mi` (the browser declares 1024 there and a
        wake that does not fit returns 402 naming needs_mi/headroom_mi/
        upgrade_to). A manifest value that disagrees with the operator's would
        show the user one number and refuse them on another — keep them equal.
      - apps that declare nothing show NO number in the store, by design.
        Absent data renders as absent; the store never invents a figure.
    """
    baseline_memory_mi: int | None = None
    # vcpus — the CPU ceiling a process app's own pod runs at, whole vCPUs
    # from 1 to VCPUS_MAX. Unset is 1, what every app pod ran at before.
    # Billed as it runs: the operator renders the ceiling and bills each
    # window compute_s x vcpus (the harnesses' rule: no unbilled compute). A
    # renderer that uses every core (Blender) finishes sooner at a higher
    # ceiling for about the same compute bill.
    vcpus: int | None = None


# The bridge prices vcpus 1..8 (harness.compute_charge; the operator's
# _HARNESS_CPU_LIMIT_MAX_M mirrors it), so no app may render above it.
VCPUS_MAX = 8


# ── The recipe ──────────────────────────────────────────────────────────────
# What the store distributes is a RECIPE, not an executable: where the code
# came from, how it is assembled, and under what terms. `source` and `build`
# are the two blocks that make an app reproducible on the installer's machine;
# the platform stamps the values only it can know (the commit it packed, the
# hash of the lockfile it packed) at pack time, and those stamped values are
# refused in a hand-written source app.yaml (PLATFORM_INJECTED_NESTED_KEYS).

# owner/name of a repo on the platform Gitea. Slug characters only — the value
# becomes a URL segment on the listing.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEX_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# An SPDX-style licence id: letters, digits, dot, plus, dash ("GPL-3.0",
# "MIT", "Apache-2.0", "CC-BY-NC-4.0"). Matched, never interpreted, here.
_SPDX_RE = re.compile(r"^[A-Za-z0-9.+-]{1,64}$")


@dataclass(frozen=True)
class UpstreamSpec:
    """Where the app's code came from, when it did not start here.

    url      — the upstream repository (https only; shown on the listing as
               the app's Source link, so a buyer sees the open-source origin).
    ref      — the branch/tag/commit the app was taken from. Informational.
    license  — the upstream's licence as an SPDX-style id. REQUIRED when url
               is set: the store shows it, and the publish gate reads it. The
               platform checks it against the upstream's LICENSE file at pack
               time when the repo is reachable; a mismatch is a refusal there.
    """
    url: str | None = None
    ref: str | None = None
    license: str | None = None


@dataclass(frozen=True)
class SourceSpec:
    """The app's own source: which platform repo and which commit.

    repo     — owner/name on the platform Gitea (the project's repo).
    commit   — PLATFORM-STAMPED at pack time (the commit the bundle was packed
               from). Refused in a source app.yaml; present in every packed
               bundle of a repo-backed project.
    tree     — PLATFORM-STAMPED: the git tree hash of that commit, which the
               runtime pod verifies against the source it fetches.
    upstream — where the code came from before it was here (UpstreamSpec).
    """
    repo: str | None = None
    commit: str | None = None
    tree: str | None = None
    upstream: UpstreamSpec = field(default_factory=UpstreamSpec)


@dataclass(frozen=True)
class BuildSpec:
    """How the app's dependencies are assembled — the pinned half of the recipe.

    lockfile        — path (relative to the app root) of a hashed lockfile the
                      runtime pod installs from with --require-hashes. Only a
                      runtime.image app has a build; a static app declaring one
                      is refused at load. REQUIRED at store admission for a
                      process app (validate_publish) — an unpinned dependency
                      fetched at first boot is the one input the recipe would
                      otherwise not prove.
    lockfile_sha256 — PLATFORM-STAMPED at pack time; refused in a source
                      app.yaml. The admission bar recomputes it from the packed
                      bytes and refuses a mismatch.
    """
    lockfile: str | None = None
    lockfile_sha256: str | None = None


# (block, key) pairs the platform writes at pack time and an author may never
# write. validate_build refuses a source app.yaml carrying any of them, for
# the same reason validated_under_sdk is refused in a source tree: a value
# that means "the platform verified this" must not be claimable by hand.
PLATFORM_INJECTED_NESTED_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("source", "commit"), ("source", "tree"), ("build", "lockfile_sha256"),
})


# The fork policies the manifest offers. The store catalog's own column accepts
# a third value, 'invited', which is gated behind circles — the manifest
# deliberately does not offer it yet, because a value the store cannot act on
# would be a setting the author believes is in force and is not.
ALLOWED_FLAVOR_POLICY: frozenset[str] = frozenset({"open", "closed"})


@dataclass(frozen=True)
class LicenseSpec:
    """What the AUTHOR permits an INSTALLER to do with this app.

    The app's licence, declared by its author in app.yaml and therefore covered
    by the bundle signature — it cannot be edited after review, and it travels
    with the app to every machine that installs it.

    It binds INSTALLERS, never the author. Publishing a site from your own
    project is always yours to do; these fields govern only what someone ELSE
    may do with your work once they have it. That is why the two rights default
    to DENY: silence from an author means "I did not grant this", not "help
    yourself". `flavors` defaults to 'open' instead, to match the store
    catalog's own fork_policy default — a manifest default that disagreed with
    the column would have the app's licence and its catalog row saying
    different things about the same right.

    publish_site   — may an installer publish a WEBSITE built from this app?
                     Enforced in routes/applications.py::_license_gate_site.
    redistribute   — may an installer republish this app to the store under
                     their own name? Refused at publish when false and the
                     submission descends from this app (the publish gate).
    flavors        — 'open': someone may publish a FLAVOUR (a child of this
                     app, lineage recorded, the parent's author still named).
                     'closed': no published flavours.
    source_visible — may the store listing link to this app's PLATFORM repo?
                     Default false: the author opts in. Governs a link on
                     the listing only — an installer always has the source
                     on their own machine (the runtime runs it), and reading
                     it there is never gated.
    notes          — the author's own words, shown at install. Never parsed;
                     it is a message, not a rule.
    """
    publish_site: bool = False
    redistribute: bool = False
    flavors: str = "open"
    source_visible: bool = False
    notes: str = ""


@dataclass(frozen=True)
class AppManifest:
    id: str
    name: str
    entrypoint: str
    # provenance = the app's trust class {native, standard, community, custom}.
    # Renamed from `tier` (the word collided with the billing tier).
    provenance: str = "standard"
    permissions: list[str] = field(default_factory=list)
    description: str = ""
    icon: str | None = None
    icon_small: str | None = None
    version: str = "0.0.0"
    system: bool = False
    window: WindowHints = field(default_factory=WindowHints)
    runtime: RuntimeSpec = field(default_factory=RuntimeSpec)
    storage: StorageSpec = field(default_factory=StorageSpec)
    resources: ResourcesSpec = field(default_factory=ResourcesSpec)
    prompt_behavior: PromptBehavior = field(default_factory=PromptBehavior)
    source_dir: Path | None = None
    # Optional install restriction: if non-empty, the factory installer
    # only installs this app when ENTITY_ID is in the list. Empty list
    # (default) means install on every entity. Used to ship entity-flavored
    # apps (e.g. a NOC dashboard scoped to one security entity).
    target_entities: list[int] = field(default_factory=list)
    # External hosts the app is allowed to call. Drives the per-app CSP
    # connect-src for isolated/published apps — an undeclared host is blocked by
    # the browser. Empty = the app's own origin /api only. Bare https hostnames,
    # no scheme/path/wildcard.
    network: list[str] = field(default_factory=list)
    # Opt-in inline-script hardening for a published (community) app. When true
    # AND the served HTML carries no un-hashable inline handlers, proxy.py pins
    # script-src to the sha256 hashes of the app's OWN inline scripts and DROPS
    # 'unsafe-inline', so injected script can't run (proxy.py _app_meta /
    # _app_csp). proxy.py reads it straight off the raw signature-covered YAML;
    # it is declared here too so it is a real, discoverable schema field rather
    # than an undocumented string only the CSP path knows about.
    csp_strict_inline: bool = False
    # `deprecated: true` in app.yaml surfaces a badge in the App Store and
    # an "(deprecated)" suffix in the dashboard launcher. The app still
    # works — this is signal, not gating.
    deprecated: bool = False
    # `operator_only: true` marks an OPERATOR-only app: it is filtered from the
    # user launcher and cannot be tier=native. (No app uses this today — the flag
    # is a kept capability for a future operator-only app.) `platform` is the
    # LEGACY name for the same flag,
    # kept for back-compat with already-shipped manifests — read both via
    # server.applications.identity.identity(); never check these fields directly.
    operator_only: bool = False
    platform: bool = False  # DEPRECATED alias for operator_only
    # The author's licence — what an INSTALLER may do with this app. Absent
    # from a manifest means the defaults in LicenseSpec, which deny the two
    # transferable rights. Adding it here is what puts `license` into
    # DECLARABLE_KEYS (that set is derived from these fields), so the key is
    # accepted by the admission gates the moment it is readable by the loader.
    license: LicenseSpec = field(default_factory=LicenseSpec)
    # The recipe: where the code is from and how it is assembled. See
    # SourceSpec / BuildSpec. Absent = an app with no repo behind it (a
    # first-party image bundle, a hand-dropped zip); the store bar decides
    # what it requires of each runtime kind.
    source: SourceSpec = field(default_factory=SourceSpec)
    build: BuildSpec = field(default_factory=BuildSpec)
    # The app-data contract. Absent = the app is not data-bearing and keeps
    # nothing but its own key/value config. Present = it declares the tables its
    # rows live in and the column in each that names the Authentik subject who
    # owns the row. See DataSpec.
    data: DataSpec = field(default_factory=DataSpec)
    # Live context (mastercode.live_context): cards this app
    # publishes into its Host's prompt — each reads only this app's own
    # declared data.tables and columns, rendered by code with no model. The
    # validated specs (live_context.normalize_card_spec), in declared order.
    live_context: tuple = ()
    # An MCP server the app runs for its own Host. See McpSpec.
    mcp: McpSpec = field(default_factory=McpSpec)

    @property
    def tier(self) -> str:
        """PERMANENT read-only alias for `provenance` (the field's older name).
        Back-compat for any external/legacy reader; new code uses `.provenance`."""
        return self.provenance


class ManifestError(ValueError):
    """Raised when an app.yaml is invalid."""


def runs_in_pod(manifest: AppManifest) -> bool:
    """THE placement predicate: does this app's server side run in its own
    runtime pod (the app-runtime wake/release lane), or beside the kernel?

    True for every helper app and for every process app that names a
    toolchain (`runtime.image`). False for static/external apps and for the
    image-baked first-party process apps. Every caller that must know where
    an app runs — launch, close, the proxy, the runtime switch, the boot
    reconcile, the descriptor sync — asks this, so placement is decided in
    exactly one place and can never disagree between them."""
    rt = manifest.runtime
    return rt.kind == "helper" or (rt.kind == "process" and rt.image is not None)


# --------------------------------------------------------------------------
# The key schema — what an app.yaml may contain.
# --------------------------------------------------------------------------
# DECLARABLE_KEYS is the author-facing surface: every key an app may write, and
# nothing else. It is derived from the AppManifest dataclass so it can never
# drift from the fields the loader actually reads (source_dir is excluded — the
# loader fills it in from the file's own location, it is never declared).
DECLARABLE_KEYS: frozenset[str] = frozenset(
    f.name for f in fields(AppManifest) if f.name != "source_dir"
) - {"platform"}

# Keys that are NOT part of the schema but are permanently accepted, because
# refusing them would refuse manifests that already exist and cannot be rewritten:
#   tier      — the older spelling of `provenance`, signature-covered in
#               already-published bundles (kernel.PROVENANCE_MANIFEST_KEYS).
#   platform  — the pre-rename spelling of `operator_only`, same reason.
#   price_usd / billing / pricing — App Store listing metadata carried in shipped
#               and signed bundles (system-apps/{browser,circles,training-memory}).
#               The kernel reads none of them; they ride along in the signed bytes.
# They are deliberately absent from DECLARABLE_KEYS: accepted, not advertised.
LEGACY_KEYS: frozenset[str] = frozenset({"tier", "platform"})
STORE_METADATA_KEYS: frozenset[str] = frozenset({"price_usd", "billing", "pricing"})

# Written INTO the manifest by the platform, never by the author:
#   validated_under_sdk — the {version, hash} of the App SDK that validated the
#       build, stamped in at sign time (routes/applications.py, factory ship path)
#       so the binding is signature-covered. It is therefore present in every
#       signed bundle and absent from every source app.yaml — the publish gate
#       requires it (validate_publish), which is exactly why it must be accepted
#       here: refusing it would refuse every bundle the platform itself produced.
PLATFORM_INJECTED_KEYS: frozenset[str] = frozenset({"validated_under_sdk"})

# The full accept set for the unknown-key check.
KNOWN_MANIFEST_KEYS: frozenset[str] = (
    DECLARABLE_KEYS | LEGACY_KEYS | STORE_METADATA_KEYS | PLATFORM_INJECTED_KEYS
)


def unknown_keys(raw: dict[str, Any]) -> list[str]:
    """Top-level app.yaml keys that are not part of the manifest schema, sorted.

    Called by the two admission gates (build_contract.validate_build and
    .validate_publish) so an invented key is a clear, actionable error at the
    moment the app is written — instead of being silently ignored forever, which
    is how an app ends up shipping a setting it believes is in force. Returns []
    for anything that isn't a mapping (the caller reports that separately)."""
    if not isinstance(raw, dict):
        return []
    return sorted(str(k) for k in raw if str(k) not in KNOWN_MANIFEST_KEYS)


# A bare, unquoted, lowercase SQL identifier. Deliberately narrower than what
# Postgres accepts: these names are interpolated into the keyed COPY the move op
# runs, and a name needing quotes — or carrying a schema qualifier, a space, or
# mixed case — is one that reads differently depending on who quotes it. The
# contract takes the subset that means the same thing everywhere.
_SQL_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# PostgreSQL RESERVED key words — the ones that cannot be used as a table or
# column name without double-quoting. Source: PostgreSQL 16 documentation,
# Appendix C "SQL Key Words", the rows marked `reserved` in the PostgreSQL
# column (the `non-reserved` and `reserved (can be function or type name)` rows
# are deliberately NOT here — `name`, `value`, `text`, `type`, `key` and friends
# are ordinary identifiers and refusing them would be a gratuitous tax).
#
# Hardcoded rather than derived, because there is no stdlib source for it and a
# runtime query would make the contract depend on a database being reachable at
# validation time — which it is not: this gate runs container-free.
#
# WHY THE CONTRACT CARES. Step 4's export interpolates these names into a
# `COPY <table> (<columns>) ... WHERE <subject_column> = $1`. Unquoted, a table
# called `user` or a column called `order` is a syntax error at the first COPY —
# i.e. the app validates, ships, and then the one operation this whole contract
# exists to enable fails on a name nobody looked at. Quoting everywhere would
# work too, but then the declared name and the real name differ by who quoted
# it, and the narrow-identifier rule above exists precisely to stop that.
PG_RESERVED_WORDS: frozenset[str] = frozenset({
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
    "asymmetric", "both", "case", "cast", "check", "collate", "column",
    "constraint", "create", "current_catalog", "current_date", "current_role",
    "current_time", "current_timestamp", "current_user", "default",
    "deferrable", "desc", "distinct", "do", "else", "end", "except", "false",
    "fetch", "for", "foreign", "from", "grant", "group", "having", "in",
    "initially", "intersect", "into", "lateral", "leading", "limit",
    "localtime", "localtimestamp", "not", "null", "offset", "on", "only", "or",
    "order", "placing", "primary", "references", "returning", "select",
    "session_user", "some", "symmetric", "system_user", "table", "then", "to",
    "trailing", "true", "union", "unique", "user", "using", "variadic", "when",
    "where", "window", "with",
})

DATA_KEYS: frozenset[str] = frozenset({"subject_column", "tables"})
DATA_TABLE_KEYS: frozenset[str] = frozenset({"name", "columns", "subject_column"})


def _bad_ident(name: Any) -> str:
    """Why `name` is unusable as a bare table/column name, or "" if it is fine."""
    if not isinstance(name, str) or not _SQL_IDENT_RE.match(name):
        return (f"must be a bare lowercase identifier matching "
                f"{_SQL_IDENT_RE.pattern}, got {name!r}")
    if name in PG_RESERVED_WORDS:
        return (f"{name!r} is a PostgreSQL reserved word and would need quoting "
                f"everywhere it is used — the keyed export writes it unquoted, "
                f"so rename it (e.g. {name}_ or app_{name})")
    return ""


def data_contract_violations(raw: dict[str, Any]) -> list[str]:
    """Everything wrong with a manifest's `data` block, in order, as sentences.

    THE ONE RULE THIS ENFORCES: every table a data-bearing app declares carries
    the owner-subject column, and that column is really one of the table's own
    columns. A table naming a subject column it does not have would declare an
    ownership key nothing can select on — the move would either fail at the
    first COPY or, far worse, fall back to moving the table whole, which is
    every other member's rows.

    Returns [] for a manifest with no `data` block: not declaring data is a
    legitimate state (most apps keep nothing but their own config). What is not
    legitimate is declaring tables and leaving the ownership key off them.

    Pure and side-effect free, over the RAW mapping, so both admission gates can
    use it: load() raises on the first violation (build path), validate_publish
    reports them all at once (store path).
    """
    if not isinstance(raw, dict):
        return []
    blk = raw.get("data")
    if blk is None:
        return []
    if not isinstance(blk, dict):
        return [f"data must be a mapping, got {type(blk).__name__}"]

    out: list[str] = []
    unknown = sorted(str(k) for k in blk if str(k) not in DATA_KEYS)
    if unknown:
        out.append(f"unknown data key(s) {unknown} (allowed: {sorted(DATA_KEYS)})")

    default_subject = blk.get("subject_column", "owner_subject")
    _why = _bad_ident(default_subject)
    if _why:
        out.append(f"data.subject_column {_why} — this is the column the move "
                   f"selects on, so it may not be empty, quoted or qualified")
        default_subject = ""

    tables = blk.get("tables")
    if tables is None:
        if "tables" in blk:
            out.append("data.tables must be a list of tables, got null")
        # `data:` with only a subject_column declares an ownership key for no
        # tables. That is a manifest that believes it is data-bearing and is
        # not, which is the misunderstanding worth naming out loud.
        elif not out:
            out.append(
                "data declares no tables — remove the `data` block, or list the "
                "tables this app keeps rows in under data.tables")
        return out
    if not isinstance(tables, list):
        return out + [f"data.tables must be a list, got {type(tables).__name__}"]
    if not tables:
        return out + ["data.tables is empty — remove the `data` block instead, "
                      "so 'not data-bearing' is one state and not two"]

    seen: set[str] = set()
    for i, t in enumerate(tables):
        where = f"data.tables[{i}]"
        if not isinstance(t, dict):
            out.append(f"{where} must be a mapping with a name and columns, "
                       f"got {type(t).__name__}")
            continue
        t_unknown = sorted(str(k) for k in t if str(k) not in DATA_TABLE_KEYS)
        if t_unknown:
            out.append(f"{where} has unknown key(s) {t_unknown} "
                       f"(allowed: {sorted(DATA_TABLE_KEYS)})")
        name = t.get("name")
        _why = _bad_ident(name)
        if _why:
            out.append(f"{where}.name {_why}")
            name = ""
        elif name in seen:
            out.append(f"{where}.name {name!r} is declared twice — each table "
                       f"appears once, or the move would export it twice")
        else:
            seen.add(name)
        where = f"data.tables[{name or i}]"

        cols = t.get("columns")
        if not isinstance(cols, list) or not cols:
            out.append(f"{where}.columns must be a non-empty list of column "
                       f"names — the move copies these columns by name, so an "
                       f"undeclared column is a column that does not travel")
            continue
        for c in cols:
            _why = _bad_ident(c)
            if _why:
                out.append(f"{where}.columns has an invalid name: {_why}")
        dupes = sorted({c for c in cols if isinstance(c, str)
                        and cols.count(c) > 1})
        if dupes:
            out.append(f"{where}.columns lists {dupes} more than once")

        subject = t.get("subject_column", default_subject)
        _why = _bad_ident(subject)
        if _why:
            out.append(f"{where}.subject_column {_why}")
            continue
        # THE CHECK. Everything above is shape; this is the contract.
        if subject not in [c for c in cols if isinstance(c, str)]:
            out.append(
                f"{where} declares subject_column {subject!r}, which is not one "
                f"of its columns {sorted(c for c in cols if isinstance(c, str))!r}. "
                f"Every table of a data-bearing app carries the owner-subject "
                f"column from its first migration — add {subject!r} to the table "
                f"and to this list, or name the column that already holds the "
                f"owner's Authentik subject")
    return out


LIVE_CONTEXT_MAX_CARDS = 8


def _declared_tables(raw: dict[str, Any]) -> dict[str, set[str]]:
    blk = raw.get("data") if isinstance(raw, dict) else None
    out: dict[str, set[str]] = {}
    if isinstance(blk, dict):
        for t in (blk.get("tables") or []):
            if isinstance(t, dict) and t.get("name"):
                out[str(t["name"])] = {str(c) for c in (t.get("columns") or [])}
    return out


def live_context_cards(raw: dict[str, Any]) -> tuple[tuple, list[str]]:
    """(validated card specs, violations) for an app's `live_context:` block.
    A card may read only this app's own declared tables and columns
    (live_context.normalize_card_spec). Absent = no cards."""
    blk = raw.get("live_context") if isinstance(raw, dict) else None
    if blk is None:
        return (), []
    if not isinstance(blk, list):
        return (), ["live_context must be a list of cards"]
    if len(blk) > LIVE_CONTEXT_MAX_CARDS:
        return (), [f"live_context: at most {LIVE_CONTEXT_MAX_CARDS} cards"]
    from server.applications.live_context_spec import normalize_card_spec
    tables = _declared_tables(raw)
    specs, errs, seen = [], [], set()
    for i, card in enumerate(blk):
        spec, e = normalize_card_spec(card, tables=tables, app_id=str(raw.get("id") or ""))
        errs.extend(f"live_context[{i}]: {m}" for m in e)
        if spec:
            if spec["id"] in seen:
                errs.append(f"live_context[{i}]: duplicate id {spec['id']!r}")
            seen.add(spec["id"])
            specs.append(spec)
    return (tuple(specs) if not errs else ()), errs


def _data_spec(raw: dict[str, Any]) -> DataSpec:
    """Build the DataSpec from an ALREADY-VALIDATED raw mapping."""
    blk = raw.get("data")
    if not isinstance(blk, dict):
        return DataSpec()
    default_subject = str(blk.get("subject_column", "owner_subject"))
    tables = []
    for t in (blk.get("tables") or []):
        tables.append(TableSpec(
            name=str(t["name"]),
            columns=tuple(str(c) for c in (t.get("columns") or [])),
            subject_column=str(t.get("subject_column", default_subject)),
        ))
    return DataSpec(subject_column=default_subject, tables=tuple(tables))


def load(yaml_path: Path) -> AppManifest:
    if not yaml_path.exists():
        raise ManifestError(f"manifest not found: {yaml_path}")

    raw: dict[str, Any] = yaml.safe_load(yaml_path.read_text()) or {}

    missing = [f for f in REQUIRED_FIELDS if not raw.get(f)]
    if missing:
        raise ManifestError(f"{yaml_path}: missing required field(s): {missing}")

    app_id = str(raw["id"])
    if not _ID_RE.match(app_id):
        raise ManifestError(
            f"{yaml_path}: invalid id {app_id!r} — must be a lowercase slug "
            f"[a-z0-9-], 1-64 chars, not starting with '-' (it is used as a path "
            f"and URL segment, so traversal/odd characters are refused)"
        )
    if app_id in RESERVED_APP_IDS:
        raise ManifestError(
            f"{yaml_path}: id {app_id!r} is reserved — an app token carrying it "
            f"would be read as the host or the shell, not as an app"
        )

    # provenance — read the new key, fall back to the legacy `tier` key for
    # already-published, immutable `tier:`-signed bundles. PERMANENT back-compat
    # (the field is signature-covered, so legacy bundles can't be rewritten; a
    # fleet with hibernated entities can never guarantee zero of them). `premium`
    # was dropped (dead vocabulary == standard); coerce any lingering premium bundle.
    provenance = DEFAULT_PROVENANCE
    for _k in PROVENANCE_MANIFEST_KEYS:
        if raw.get(_k):
            provenance = str(raw[_k])
            break
    if provenance == "premium":
        provenance = "standard"
    if provenance not in ALLOWED_PROVENANCE:
        raise ManifestError(
            f"{yaml_path}: unknown provenance {provenance!r} "
            f"(allowed: {sorted(ALLOWED_PROVENANCE)})"
        )
    if provenance == "native" and app_id not in KERNEL_REQUIRED_APPS:
        raise ManifestError(
            f"{yaml_path}: id {app_id!r} claims provenance=native but is not "
            f"in kernel KERNEL_REQUIRED_APPS — refuse to install"
        )

    # operator_only is the canonical flag; `platform` is the legacy alias — accept
    # either so already-shipped manifests keep working through the rename.
    platform = bool(raw.get("platform", False))
    operator_only = bool(raw.get("operator_only", False)) or platform
    if operator_only and provenance == "native":
        raise ManifestError(
            f"{yaml_path}: id {app_id!r} is operator_only AND provenance=native — an "
            f"operator-only app must not be native (native apps are baked onto "
            f"every host and shown to users). Pick one."
        )

    permissions = list(raw.get("permissions") or [])
    try:
        validate_permissions(permissions)
    except ValueError as e:
        raise ManifestError(f"{yaml_path}: {e}") from None
    # A retired permission is accepted (an app published before it retired
    # still loads) and dropped here, so it grants nothing and is never shown.
    from server.applications.scopes import RETIRED_PERMISSIONS
    permissions = [p for p in permissions if p not in RETIRED_PERMISSIONS]

    win_raw = raw.get("window") or {}
    win_mobile = str(win_raw.get("mobile") or "scale")
    if win_mobile not in ("scale", "responsive"):
        raise ManifestError(
            f"{yaml_path}: unknown window.mobile {win_mobile!r} "
            f"(allowed: ['responsive', 'scale'])"
        )
    window = WindowHints(
        title=win_raw.get("title"),
        width=win_raw.get("width"),
        height=win_raw.get("height"),
        resizable=bool(win_raw.get("resizable", True)),
        min_width=win_raw.get("min_width"),
        min_height=win_raw.get("min_height"),
        start_maximized=bool(win_raw.get("start_maximized", False)),
        mobile=win_mobile,
    )

    rt_raw = raw.get("runtime") or {}
    rt_kind = str(rt_raw.get("kind") or "static")
    if rt_kind not in ALLOWED_RUNTIME_KINDS:
        raise ManifestError(
            f"{yaml_path}: unknown runtime.kind {rt_kind!r} "
            f"(allowed: {sorted(ALLOWED_RUNTIME_KINDS)})"
        )
    if rt_kind == "process":
        cmd = rt_raw.get("command")
        if not isinstance(cmd, list) or not cmd:
            raise ManifestError(
                f"{yaml_path}: runtime.kind=process requires "
                f"runtime.command: [argv, ...]"
            )
        command = [str(x) for x in cmd]
    else:
        command = []

    # runtime.image — placement, decided by the manifest and nothing else.
    rt_image: str | None = None
    if rt_raw.get("image") is not None:
        if rt_kind != "process":
            raise ManifestError(
                f"{yaml_path}: runtime.image applies to runtime.kind=process only "
                f"(a {rt_kind} app has no process to give a toolchain)")
        rt_image = str(rt_raw["image"]).strip().lower()
        if rt_image not in ALLOWED_RUNTIME_IMAGES:
            raise ManifestError(
                f"{yaml_path}: unknown runtime.image {rt_image!r} "
                f"(allowed: {sorted(ALLOWED_RUNTIME_IMAGES)})")
    elif rt_kind == "process" and provenance not in IMAGE_BAKED_PROVENANCES:
        raise ManifestError(
            f"{yaml_path}: runtime.kind=process with provenance={provenance!r} "
            f"requires runtime.image (one of {sorted(ALLOWED_RUNTIME_IMAGES)}) — "
            f"user code runs in its own runtime pod, and the image names the "
            f"toolchain that pod is built from")

    rt_lifecycle = str(rt_raw.get("lifecycle") or "window")
    if rt_lifecycle not in ALLOWED_RUNTIME_LIFECYCLES:
        raise ManifestError(
            f"{yaml_path}: unknown runtime.lifecycle {rt_lifecycle!r} "
            f"(allowed: {sorted(ALLOWED_RUNTIME_LIFECYCLES)})"
        )
    if rt_lifecycle == "resident" and rt_kind != "process":
        raise ManifestError(
            f"{yaml_path}: runtime.lifecycle=resident needs runtime.kind=process "
            f"(a {rt_kind} app has no process to keep resident)"
        )

    rt_port: int | None = None
    if rt_kind == "external":
        raw_port = rt_raw.get("port")
        if not isinstance(raw_port, int) or raw_port <= 0 or raw_port > 65535:
            raise ManifestError(
                f"{yaml_path}: runtime.kind=external requires "
                f"runtime.port: <int 1-65535>"
            )
        rt_port = raw_port

    rt_ttls: dict[str, int] = {}
    ttls_raw = rt_raw.get("ttls") or {}
    if not isinstance(ttls_raw, dict):
        raise ManifestError(f"{yaml_path}: runtime.ttls must be a mapping of host -> seconds")
    for th, tv in ttls_raw.items():
        try:
            tv_i = int(tv)
        except (TypeError, ValueError):
            raise ManifestError(
                f"{yaml_path}: runtime.ttls[{th!r}] must be an integer (seconds)")
        if tv_i <= 0:
            raise ManifestError(
                f"{yaml_path}: runtime.ttls[{th!r}] must be positive")
        rt_ttls[str(th)] = tv_i

    runtime = RuntimeSpec(
        kind=rt_kind,
        lifecycle=rt_lifecycle,
        image=rt_image,
        command=command,
        port_env=str(rt_raw.get("port_env") or "PORT"),
        ready_path=str(rt_raw.get("ready_path") or "/health"),
        ready_timeout_seconds=int(
            rt_raw.get("ready_timeout_seconds") or DEFAULT_READY_TIMEOUT
        ),
        port=rt_port,
        strip_prefix=bool(rt_raw.get("strip_prefix", True)),
        harness=(str(rt_raw.get("harness")).strip() if rt_raw.get("harness") else None),
        ttls=rt_ttls,
    )

    st_raw = raw.get("storage") or {}
    storage = StorageSpec(
        workspace=bool(st_raw.get("workspace", False)),
    )

    # resources — the declared machine cost (see ResourcesSpec). Validated
    # strictly: a malformed figure silently coerced to None would make the
    # store show "no cost" for an app whose author DID declare one, which is
    # the exact dishonesty the field exists to prevent.
    res_raw = raw.get("resources") or {}
    if not isinstance(res_raw, dict):
        raise ManifestError(f"{yaml_path}: resources must be a mapping")
    baseline_mi: int | None = None
    if res_raw.get("baseline_memory_mi") is not None:
        bm = res_raw["baseline_memory_mi"]
        if not isinstance(bm, int) or isinstance(bm, bool) or bm <= 0:
            raise ManifestError(
                f"{yaml_path}: resources.baseline_memory_mi must be a "
                f"positive integer (MiB), got {bm!r}"
            )
        baseline_mi = bm
    vcpus: int | None = None
    if res_raw.get("vcpus") is not None:
        vc = res_raw["vcpus"]
        if not isinstance(vc, int) or isinstance(vc, bool) or not 1 <= vc <= VCPUS_MAX:
            raise ManifestError(
                f"{yaml_path}: resources.vcpus must be a whole number from 1 to {VCPUS_MAX}, "
                f"got {vc!r}")
        rt_kind = (raw.get("runtime") or {}).get("kind") if isinstance(raw.get("runtime"), dict) else None
        if rt_kind != "process":
            raise ManifestError(
                f"{yaml_path}: resources.vcpus is the CPU ceiling of a process app's own pod "
                f"(runtime.kind: process)")
        vcpus = vc
    resources = ResourcesSpec(baseline_memory_mi=baseline_mi, vcpus=vcpus)

    pb_raw = raw.get("prompt_behavior") or {}
    if not isinstance(pb_raw, dict):
        raise ManifestError(
            f"{yaml_path}: prompt_behavior must be a mapping"
        )
    # A manifest already published may still carry the coaching list or a
    # longer paragraph; it loads, and the prompt never carries either: the
    # list is ignored and the paragraph is cut at its last sentence inside the
    # cap. This repo's own apps are held to the rule by
    # scripts/test_prompt_behavior_truth.py.
    _what = clip_what_it_is(str(pb_raw.get("what_it_is") or "").strip())
    prompt_behavior = PromptBehavior(
        what_it_is=_what,
        project_scoped=bool(pb_raw.get("project_scoped", False)),
        user_path=str(pb_raw.get("user_path") or "").strip(),
        step_up=str(pb_raw.get("step_up") or "").strip(),
    )

    target_entities_raw = raw.get("target_entities") or []
    if not isinstance(target_entities_raw, list):
        raise ManifestError(
            f"{yaml_path}: target_entities must be a list of ints"
        )
    try:
        target_entities = [int(x) for x in target_entities_raw]
    except (TypeError, ValueError):
        raise ManifestError(
            f"{yaml_path}: target_entities entries must be ints"
        )

    network_raw = raw.get("network") or []
    if not isinstance(network_raw, list):
        raise ManifestError(f"{yaml_path}: network must be a list of hostnames")
    network: list[str] = []
    for h in network_raw:
        h = str(h).strip()
        if not h:
            continue
        if "://" in h or "/" in h or "*" in h or " " in h:
            raise ManifestError(
                f"{yaml_path}: network host {h!r} must be a bare hostname "
                f"(no scheme, path, or wildcard)"
            )
        network.append(h)

    # helper cross-field contract: the descriptor the installer writes is
    # {budgetMi: baseline_memory_mi, network} — a helper app missing either
    # would install and then be un-provisionable, which reads as "the app is
    # broken" rather than "the manifest is incomplete". Refused HERE so the
    # failure happens at author time with the actual missing field named.
    if rt_kind == "helper":
        if not resources.baseline_memory_mi:
            raise ManifestError(
                f"{yaml_path}: runtime.kind=helper requires "
                f"resources.baseline_memory_mi (the pod's declared budget)")
        if resources.baseline_memory_mi < APP_POD_REQUEST_MI["helper"]:
            raise ManifestError(
                f"{yaml_path}: resources.baseline_memory_mi is the helper pod's memory "
                f"limit and must be at least {APP_POD_REQUEST_MI['helper']} (the memory "
                f"the pod reserves); a lower limit is refused and the app never starts")
        if not network:
            raise ManifestError(
                f"{yaml_path}: runtime.kind=helper requires a non-empty "
                f"network[] (the helper's host allowlist IS the app's wall)")
    # A process app that runs in its own pod: the descriptor is {budgetMi,
    # network, image, command}, and the pod's memory limit IS
    # baseline_memory_mi. An app missing its budget would install and then be
    # un-provisionable — refused here, at author time, naming the field.
    # Its network[] may be EMPTY: the app reaches nothing outside the platform
    # (a game, a calculator, anything that keeps its own state). The pod's
    # wall treats an empty list as exactly that (sidecars/app-egress/egress.py,
    # "this app reaches nothing external"), the pod's network policies never
    # read the list, and the app still reaches its Host through the cluster
    # resolver. It used to be refused here, so an app that honestly reached
    # nothing could only install by naming a host it never uses, which opens
    # the pod to that host. A helper is
    # different: it exists to fetch from its hosts, so it keeps the rule above.
    if rt_image is not None:
        if not resources.baseline_memory_mi:
            raise ManifestError(
                f"{yaml_path}: runtime.image={rt_image!r} requires "
                f"resources.baseline_memory_mi (the runtime pod's memory limit)")
        if resources.baseline_memory_mi < APP_POD_REQUEST_MI["process"]:
            raise ManifestError(
                f"{yaml_path}: resources.baseline_memory_mi is the runtime pod's memory "
                f"limit and must be at least {APP_POD_REQUEST_MI['process']} (the memory "
                f"the pod reserves); a lower limit is refused and the app never starts")

    # licence — the author's terms for an installer. Unknown keys inside the
    # block are refused for the same reason unknown_keys() refuses them at the
    # top level: a misspelled right that is silently ignored is a permission
    # the author believes they withheld.
    lic_raw = raw.get("license") or {}
    if not isinstance(lic_raw, dict):
        raise ManifestError(f"{yaml_path}: license must be a mapping")
    _lic_known = {"publish_site", "redistribute", "flavors", "source_visible", "notes"}
    _lic_unknown = sorted(str(k) for k in lic_raw if str(k) not in _lic_known)
    if _lic_unknown:
        raise ManifestError(
            f"{yaml_path}: unknown license key(s) {_lic_unknown} "
            f"(allowed: {sorted(_lic_known)})"
        )
    lic_flavors = str(lic_raw.get("flavors") or "open")
    if lic_flavors not in ALLOWED_FLAVOR_POLICY:
        raise ManifestError(
            f"{yaml_path}: unknown license.flavors {lic_flavors!r} "
            f"(allowed: {sorted(ALLOWED_FLAVOR_POLICY)})"
        )
    # STRICT on the two rights, and only on those. `bool("false")` is True, so
    # a quoted `publish_site: "false"` — the shape an app-builder emits when it
    # stringifies its YAML — would have GRANTED the right its author wrote out
    # to withhold. A right is granted by a real YAML boolean or not at all;
    # anything else is refused rather than coerced in either direction.
    def _right(key: str) -> bool:
        v = lic_raw.get(key, False)
        if v is True or v is False:
            return bool(v)
        hint = ("write it as `true` or `false` with no quote marks"
                if v is None else
                "quote marks around it make it a string, and a string would "
                "silently read as a GRANTED right")
        raise ManifestError(
            f"{yaml_path}: license.{key} must be a boolean (true/false), "
            f"got {v!r} — {hint}"
        )

    app_license = LicenseSpec(
        publish_site=_right("publish_site"),
        redistribute=_right("redistribute"),
        flavors=lic_flavors,
        source_visible=_right("source_visible"),
        notes=str(lic_raw.get("notes") or "").strip(),
    )

    # source — the recipe's origin half. Strict on shape (each value is shown
    # on the listing and read by the publish gate); the platform-stamped
    # values are accepted here because load() also reads packed bundles, and
    # refused in a source tree by validate_build (PLATFORM_INJECTED_NESTED_KEYS).
    src_raw = raw.get("source") or {}
    if not isinstance(src_raw, dict):
        raise ManifestError(f"{yaml_path}: source must be a mapping")
    _src_known = {"repo", "commit", "tree", "upstream"}
    _src_unknown = sorted(str(k) for k in src_raw if str(k) not in _src_known)
    if _src_unknown:
        raise ManifestError(
            f"{yaml_path}: unknown source key(s) {_src_unknown} "
            f"(allowed: {sorted(_src_known)})")

    def _opt_str(block: dict, key: str, label: str) -> str | None:
        v = block.get(key)
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            raise ManifestError(f"{yaml_path}: {label} must be a string, got {v!r}")
        return v.strip()

    src_repo = _opt_str(src_raw, "repo", "source.repo")
    if src_repo is not None and not _REPO_RE.match(src_repo):
        raise ManifestError(
            f"{yaml_path}: source.repo {src_repo!r} must be owner/name "
            f"(slug characters only)")
    src_commit = _opt_str(src_raw, "commit", "source.commit")
    if src_commit is not None and not _HEX_RE.match(src_commit.lower()):
        raise ManifestError(
            f"{yaml_path}: source.commit {src_commit!r} must be a hex commit id")
    src_tree = _opt_str(src_raw, "tree", "source.tree")
    if src_tree is not None and not _HEX_RE.match(src_tree.lower()):
        raise ManifestError(
            f"{yaml_path}: source.tree {src_tree!r} must be a hex tree id")
    up_raw = src_raw.get("upstream") or {}
    if not isinstance(up_raw, dict):
        raise ManifestError(f"{yaml_path}: source.upstream must be a mapping")
    _up_known = {"url", "ref", "license"}
    _up_unknown = sorted(str(k) for k in up_raw if str(k) not in _up_known)
    if _up_unknown:
        raise ManifestError(
            f"{yaml_path}: unknown source.upstream key(s) {_up_unknown} "
            f"(allowed: {sorted(_up_known)})")
    up_url = _opt_str(up_raw, "url", "source.upstream.url")
    if up_url is not None and (not up_url.startswith("https://")
                               or any(c in up_url for c in " \t\n\"'<>")):
        raise ManifestError(
            f"{yaml_path}: source.upstream.url {up_url!r} must be a plain https URL")
    up_ref = _opt_str(up_raw, "ref", "source.upstream.ref")
    up_license = _opt_str(up_raw, "license", "source.upstream.license")
    if up_license is not None and not _SPDX_RE.match(up_license):
        raise ManifestError(
            f"{yaml_path}: source.upstream.license {up_license!r} must be an "
            f"SPDX-style id (e.g. MIT, GPL-3.0, Apache-2.0)")
    if up_url is not None and up_license is None:
        raise ManifestError(
            f"{yaml_path}: source.upstream.license is required when "
            f"source.upstream.url is set — the store shows the upstream's licence "
            f"and the publish gate reads it; name it as an SPDX id")
    if up_url is None and (up_ref is not None or up_license is not None):
        raise ManifestError(
            f"{yaml_path}: source.upstream.ref/license need source.upstream.url")
    app_source = SourceSpec(
        repo=src_repo, commit=(src_commit.lower() if src_commit else None),
        tree=(src_tree.lower() if src_tree else None),
        upstream=UpstreamSpec(url=up_url, ref=up_ref, license=up_license))

    # build — the recipe's assembly half. Only a runtime.image app has one.
    bld_raw = raw.get("build") or {}
    if not isinstance(bld_raw, dict):
        raise ManifestError(f"{yaml_path}: build must be a mapping")
    _bld_known = {"lockfile", "lockfile_sha256"}
    _bld_unknown = sorted(str(k) for k in bld_raw if str(k) not in _bld_known)
    if _bld_unknown:
        raise ManifestError(
            f"{yaml_path}: unknown build key(s) {_bld_unknown} "
            f"(allowed: {sorted(_bld_known)})")
    bld_lock = _opt_str(bld_raw, "lockfile", "build.lockfile")
    if bld_lock is not None:
        if (bld_lock.startswith("/") or ".." in bld_lock.split("/")
                or bld_lock.startswith("./")):
            raise ManifestError(
                f"{yaml_path}: build.lockfile {bld_lock!r} must be a plain path "
                f"relative to the app root (no leading /, no ..)")
        if rt_image is None:
            raise ManifestError(
                f"{yaml_path}: build.lockfile applies to a runtime.image app only "
                f"(a {rt_kind} app has no process to install dependencies for)")
    bld_sha = _opt_str(bld_raw, "lockfile_sha256", "build.lockfile_sha256")
    if bld_sha is not None:
        bld_sha = bld_sha.lower()
        if not _SHA256_RE.match(bld_sha):
            raise ManifestError(
                f"{yaml_path}: build.lockfile_sha256 must be a 64-hex sha256")
        if bld_lock is None:
            raise ManifestError(
                f"{yaml_path}: build.lockfile_sha256 without build.lockfile")
    app_build = BuildSpec(lockfile=bld_lock, lockfile_sha256=bld_sha)

    # data — the app-data contract. Strict here (raise) so a data-bearing app
    # whose ownership key is wrong never loads at all; validate_publish runs the
    # same function over the raw bundle and reports every violation at once.
    _data_errs = data_contract_violations(raw)
    if _data_errs:
        raise ManifestError(f"{yaml_path}: " + "; ".join(_data_errs))
    app_data = _data_spec(raw)

    # live_context — cards over this app's own declared data. Strict, like the
    # data contract it reads: a card that reaches outside it never loads.
    app_cards, _lc_errs = live_context_cards(raw)
    if _lc_errs:
        raise ManifestError(f"{yaml_path}: " + "; ".join(_lc_errs))

    app_mcp, _mcp_errs = mcp_spec(raw)
    if _mcp_errs:
        raise ManifestError(f"{yaml_path}: " + "; ".join(_mcp_errs))

    return AppManifest(
        id=app_id,
        name=str(raw["name"]),
        entrypoint=str(raw["entrypoint"]),
        provenance=provenance,
        permissions=permissions,
        network=network,
        description=str(raw.get("description") or ""),
        icon=icon_path(raw.get("icon")),
        icon_small=icon_path(raw.get("icon_small")),
        version=str(raw.get("version") or "0.0.0"),
        system=bool(raw.get("system", False)),
        window=window,
        runtime=runtime,
        storage=storage,
        resources=resources,
        prompt_behavior=prompt_behavior,
        source_dir=yaml_path.parent,
        target_entities=target_entities,
        csp_strict_inline=bool(raw.get("csp_strict_inline", False)),
        deprecated=bool(raw.get("deprecated", False)),
        operator_only=operator_only,
        platform=platform,
        license=app_license,
        source=app_source,
        build=app_build,
        data=app_data,
        live_context=app_cards,
        mcp=app_mcp,
    )


def injected_nested_keys(raw: dict[str, Any]) -> list[str]:
    """The platform-stamped nested keys present in a raw manifest, as
    'block.key' strings, sorted. Empty for a manifest an author may have
    written; non-empty means a hand-written app.yaml is claiming a value only
    the pack step may write (validate_build refuses it, naming the key)."""
    if not isinstance(raw, dict):
        return []
    out = []
    for block, key in PLATFORM_INJECTED_NESTED_KEYS:
        b = raw.get(block)
        if isinstance(b, dict) and b.get(key) not in (None, ""):
            out.append(f"{block}.{key}")
    return sorted(out)


def load_all(root: Path) -> list[AppManifest]:
    """Load every app.yaml under root. Skips invalid manifests, logging
    the error — the launcher must keep working even if one app's
    manifest is broken."""
    import logging
    log = logging.getLogger("privatae.applications.manifest")

    out: list[AppManifest] = []
    for yaml_path in sorted(root.glob("*/app.yaml")):
        try:
            out.append(load(yaml_path))
        except ManifestError as e:
            log.warning("skipping app: %s", e)
    return out
