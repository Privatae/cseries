"""What the builder is told, per ship target.

The ship target is a property of the MODULE, chosen at alignment, and it
changes the brief. Once every build was handed the App SDK contract whatever
it was for, so a public site was told to write an app.yaml and use
window.CSeriesApp — neither of which a visitor's browser has.

  private_app  the deployment contract + the App SDK contract (unchanged from
               what every build got before modules).
  store_app    the same, plus what the App Store's admission bar checks
               (build_contract.validate_publish): the scope allowlist and the
               runtime recipe rules.
  public_site  a static site served with no Host behind it: no platform APIs,
               written for readers; indexing is the owner's Visibility
               setting, never a robots.txt in the tree (the no-index-by-default
               rule).
  component    a piece of another module (a back end): no app contract at all.
  op           a program the Host runs on the user's own machines through
               their bastion: the op format,
               never an app.
  live_context cards the Host builds for the person (mastercode.live_context
               _host): one `<card id>.yaml` per card over an installed app's
               declared data; the person makes each live with one click.
  repo_change  a change to an existing code repository (a project mounted on
               or forked from one, the platform's own included): the builder
               follows the repository's own languages, layout and tests, and
               the change goes back as a pull request. Never a module target:
               it is what a mounted project's ROOT builds as (root_target),
               unless the repository has an app manifest at its root.

`checks_for(target)` says which post-run checks apply: a component has no
index.html to require and no manifest to validate.
"""
from __future__ import annotations

TARGETS = ("private_app", "public_site", "store_app", "component", "op", "live_context",
           "repo_change")
DEFAULT_TARGET = "private_app"   # a project without modules: what it always was

PUBLIC_SITE_CONTRACT = (
    "PLATFORM CONTRACT — PUBLIC SITE (AUTHORITATIVE — overrides any conflicting "
    "framework/tooling instruction that follows):\n"
    "- This is published to the web as a static site. Files are served "
    "byte-for-byte: no build step, no npm/bundler/transpile.\n"
    "- The entry point is `index.html` at the WORKTREE ROOT.\n"
    "- It runs with NO Host behind it: do not use window.CSeriesApp, do not call "
    "relative /api routes, and do not write an app.yaml — a visitor's browser has "
    "none of them. Anything dynamic calls only public https services the brief "
    "names.\n"
    "- Write it for the people who will read it: a real <title>, a meta "
    "description, <html lang>, semantic headings, alt text on images.\n"
    "- Do NOT write robots.txt or a sitemap. Whether search engines may index the "
    "site is its owner's Visibility setting in Sites (off by default); a robots "
    "file in the tree would contradict it.\n"
    "- CDN libraries are fine; your own assets are local files referenced by "
    "relative paths."
)

COMPONENT_CONTRACT = (
    "PLATFORM CONTRACT — COMPONENT: this is one piece of a larger project (for "
    "example a back end another module uses). It ships as nothing on its own: "
    "no app.yaml and no index.html are required. Work only inside your working "
    "directory, with relative paths, and follow the brief for its language and "
    "layout."
)


def _store_addendum() -> str:
    try:
        from server.applications.build_contract import COMMUNITY_ALLOWED_SCOPES
        scopes = ", ".join(sorted(COMMUNITY_ALLOWED_SCOPES))
    except Exception:  # pragma: no cover — ships beside this module
        scopes = "(the store allowlist)"
    try:
        from server.applications.manifest import ALLOWED_RUNTIME_IMAGES
        images = ", ".join(sorted(ALLOWED_RUNTIME_IMAGES))
    except Exception:  # pragma: no cover
        images = "(a platform toolchain image)"
    return (
        "STORE APP — this is published to the App Store and runs on OTHER people's "
        "Hosts. On top of the App SDK contract above, the store's admission bar "
        "checks:\n"
        f"- `permissions` only from: {scopes}. Anything else is refused at publish.\n"
        "- `runtime.kind` static or helper; or process with `runtime.image` one of "
        f"{images} and a hashed `build.lockfile` packed with the app. A pod-backed "
        "kind declares `network[]` and `resources.baseline_memory_mi`.\n"
        "- It must work for a stranger: nothing that names this user's projects, "
        "files, ids or data."
    )


OP_CONTRACT = (
    "PLATFORM CONTRACT — OP (AUTHORITATIVE — overrides any conflicting instruction "
    "that follows): this module holds programs the Host runs on the user's own "
    "machines through their bastion, never an app. No app.yaml, no index.html.\n"
    "- Layout: one directory per op, `ops/<name>/` with `op.yaml`, `run` and "
    "`test/`. <name> is lowercase letters, digits and dashes.\n"
    "- `op.yaml`: name (= the directory), version MAJOR.MINOR.PATCH, summary (ONE "
    "line, at most 200 characters, read on the person's approval card), target: "
    "machine, privilege: root | user, interpreter: /bin/sh | python3 (nothing "
    "else exists on the machine's side), time_limit in seconds (1..600), params "
    "(name -> {type: string|integer|boolean|enum|path, with max_len / min+max / "
    "values / root}), effects.files (absolute paths or globs the op may change — "
    "a trailing / means the folder and everything under it; never /, /proc, /sys, "
    "/dev, /run, /tmp, /var/tmp, /boot, and never the bastion's own config dir; "
    "NO placeholders), reads (optional: absolute paths or globs the op only "
    "reads, same rules), modes [dry_run, apply, verify] (all three; add undo when "
    "the op can reverse itself), undo: none | run (run = `run --mode undo` "
    "reverses it). Rung 6 allows file effects only: no units, packages or "
    "network effects.\n"
    "- `run` is executed as `<interpreter> run --mode <mode>` with the params as a "
    "JSON object on stdin, inside a sandbox: an overlay over the machine, no "
    "network, /proc/sys read-only, no capability beyond files. The op SEES only "
    "the system's programs and libraries, a few /etc files (passwd, group, hosts, "
    "resolv.conf, certificates) and its declared effects and reads: every other "
    "file reads as empty, so list every file run reads. Effects or reads that "
    "reach a credential store (at it, inside it, or a folder holding it, so "
    "/etc/ and /var/lib/ are refused) are refused. The stores are /root; a "
    "home's .ssh .docker .git-credentials .kube .gnupg .aws .netrc .pgpass "
    ".config; /etc/shadow /etc/gshadow (and their - backups) /etc/ssh "
    "/etc/wireguard /etc/privatae /etc/letsencrypt /etc/ssl/private /etc/pki "
    "/etc/systemd /etc/credstore /etc/credstore.encrypted /etc/docker; docker, k3s "
    "and Kubernetes state; /var/lib/cloud; and PostgreSQL and MySQL "
    "data. Name the exact folder, e.g. /etc/nginx/. dry_run and apply "
    "make the SAME changes (the sandbox is what makes the dry run harmless; the "
    "box reads what changed off the overlay and the person approves that diff); "
    "verify exits 0 when the change is in place. Write only inside effects; a "
    "symlink, a hardlink or a path outside them refuses the whole run. Be "
    "deterministic: the apply must produce exactly the change list the dry run "
    "showed, or nothing lands.\n"
    "- `test/`: how you proved it (a fixture tree and a script); it never ships to "
    "the machine.\n"
    "- Never call the network, never read secrets, never write outside effects."
)


LIVE_CONTEXT_CONTRACT = (
    "PLATFORM CONTRACT — LIVE CONTEXT CARDS (AUTHORITATIVE): this module holds "
    "cards, not code. One file per card at the module root, `<card id>.yaml`:\n"
    "- `app`: the installed app whose declared data the card reads; `id` (the file "
    "name), `title`, `keywords` (the words that bring it into a turn).\n"
    "- `reads`: `table` and `columns` from that app's declared data.tables only, "
    "and only its own prefixed tables (`<app id>__<name>`, dashes as underscores); "
    "optional `where` (column: value), `order` ('<column>' or '<column> desc'), "
    "`limit` 1-20.\n"
    "- `render`: `head` (may use {count}), `each` (may use the read columns as "
    "{column}), optional `empty`. Plain {name} placeholders only.\n"
    "- optional `refresh` (30s-24h) and `budget_chars` (100-1500).\n"
    "- Nothing else: no scripts, no queries, no index.html, no app.yaml. Each card "
    "goes live only when its owner approves the rendered preview."
)


REPO_CHANGE_CONTRACT = (
    "PLATFORM CONTRACT — REPOSITORY CHANGE (AUTHORITATIVE — overrides any "
    "conflicting instruction that follows): your working directory is a checkout "
    "of an existing code repository. You are making a reviewed change to it, and "
    "it goes back as a pull request.\n"
    "- Before editing, read the repository's own guidance (README, CLAUDE.md or "
    "AGENTS.md, CONTRIBUTING) and the code around what you change, and follow its "
    "languages, layout, build tooling and tests.\n"
    "- Change only what the task asks. Edit files in place, and add no app "
    "manifest, entry page or other scaffolding the repository does not already "
    "have.\n"
    "- Run the repository's own tests for what you touched where this environment "
    "allows, and say in your final report which ran, which could not run and why."
)


# The app manifest the build contract validates at the worktree root
# (build_contract.validate_build reads `app.yaml` there).
APP_MANIFEST = "app.yaml"


def root_target(project_config, project_dir=None) -> str | None:
    """The target of a build at a project's ROOT (no module). A project
    mounted on, or forked from, an existing repository (its config carries
    repo_seed: projects.insert_mounted_project, the ticket mount and
    fork_repo_for_project are the only writers) is a repo_change, unless the
    repository is an app: an app manifest at `project_dir`'s root keeps the
    app target, so a fix to the Host's own app repo is still held to the app
    contract, the SDK contract and the index.html entry check. Anything else
    is None (the private-app default, as before). `project_config` is
    projects.config as a dict or JSON text; `project_dir` is the project's
    directory (None = unknown, judged by the config alone)."""
    cfg = project_config
    if isinstance(cfg, str):
        try:
            import json
            cfg = json.loads(cfg)
        except ValueError:
            return None
    if isinstance(cfg, dict) and isinstance(cfg.get("repo_seed"), dict):
        if project_dir is not None:
            from pathlib import Path
            try:
                if (Path(project_dir) / APP_MANIFEST).is_file():
                    return DEFAULT_TARGET
            except OSError:
                pass
        return "repo_change"
    return None


def contract_prefix(target: str | None, deployment_contract: str, sdk_contract: str) -> str:
    """The text prepended to the builder's prompt for this target (ends with a
    blank line, or is empty). An unknown target raises: treating it as an app
    is how a repository change used to be briefed as a static site."""
    t = target or DEFAULT_TARGET
    if t not in TARGETS:
        raise ValueError(f"unknown build target {t!r}")
    if t == "repo_change":
        return REPO_CHANGE_CONTRACT + "\n\n"
    if t == "op":
        return OP_CONTRACT + "\n\n"
    if t == "live_context":
        return LIVE_CONTEXT_CONTRACT + "\n\n"
    if t == "component":
        return COMPONENT_CONTRACT + "\n\n"
    if t == "public_site":
        return PUBLIC_SITE_CONTRACT + "\n\n"
    # Only the two app targets reach the app contracts; every other target
    # returned above.
    base = deployment_contract + "\n\n" + sdk_contract + "\n\n"
    if t == "store_app":
        return base + _store_addendum() + "\n\n"
    return base


def checks_for(target: str | None) -> dict:
    """Which post-run checks apply: `entry` = an index.html must exist (the
    deployment contract's fatal checks), `manifest` = app.yaml is validated
    against the App SDK build contract."""
    t = target or DEFAULT_TARGET
    return {"entry": t not in ("component", "op", "live_context", "repo_change"),
            "manifest": t in ("private_app", "store_app")}


# The targets a harness run is briefed as an app for: the desktop design brief
# (desktop_contract) and similar app-only additions apply to these and to no
# other (review._with_desktop_brief).
APP_TARGETS = ("private_app", "store_app", "public_site")
