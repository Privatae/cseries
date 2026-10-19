"""specs — the five HarnessSpec rows driving harness_core.

Each value cites the pre-refactor system-apps/<id>/bin/main.py line it came
from; scripts/test_harness_runtime.py pins these against literal constants
lifted from those files (the golden test). Env-var knobs are read at import,
exactly when the originals read them.

No import-time side effects beyond env reads and logger creation — dir
creation happens in harness_core.register() for the ONE active spec (the
originals mkdir'd at import, but this module defines all five and must be
importable inside any single harness image, including read-only rootfs pods
where the other four cache dirs must never be touched).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tarfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .harness_core import HarnessSpec, alt_oauth_present, _read_creds
from . import host_mcp

# Streaming flags per vendor — VERIFIED 2026-07-26 by running each sidecar
# image's actual binary, not from docs. Appended to the argv only when the
# caller asks for a stream, so the buffered path stays byte-identical.
#
#   codex     --json                       "Print events to stdout as JSONL"
#   cursor    --output-format stream-json + --stream-partial-output (text deltas)
#             NOTE: the non-streaming argv passes `--output-format text`, so the
#             streaming builder REPLACES that pair rather than appending a second
#             one (two --output-format flags is a vendor-side error).
#   grok      --output-format streaming-json
#   opencode  --format json                raw JSON events
#   kimi      --output-format stream-json   (choices are exactly text|stream-json;
#             the buffered argv passes neither, so this appends cleanly)
STREAM_FLAGS = {
    "codex":    ["--json"],
    "grok":     ["--output-format", "streaming-json"],
    "opencode": ["--format", "json"],
    "cursor":   ["--output-format", "stream-json", "--stream-partial-output"],
    "kimi":     ["--output-format", "stream-json"],
    # antigravity: `--output-format text|json|stream-json` (agy 1.1.17
    # --help, 2026-08-21; unchanged in 1.3.0 --help, 2026-10-06); the buffered argv passes no --output-format, so
    # this appends cleanly. stream-json = one {"event": …} object per line.
    "antigravity": ["--output-format", "stream-json"],
}


def apply_stream_flags(harness_id: str, cmd: list) -> list:
    """Return `cmd` with this harness's streaming flags applied. Idempotent and
    non-destructive: for cursor the existing `--output-format text` pair is
    REPLACED (a duplicate flag is a vendor error), everything else appends."""
    flags = STREAM_FLAGS.get(harness_id)
    if not flags:
        return cmd
    out = list(cmd)
    if "--output-format" in out:
        i = out.index("--output-format")
        del out[i:i + 2]          # drop the buffered pair (cursor's `text`)
    return out + list(flags)

# Shared by every spec (codex main.py:64, cursor :52, grok :57, opencode :33).
WORKSPACE = Path("/var/lib/cseries/projects")

# The originals' logging setup: format identical across the four vendor CLIs
# (e.g. codex main.py:53); opencode included %(name)s (opencode main.py:28-29).
LOG_FMT = "%(asctime)s %(levelname)s %(message)s"
LOG_FMT_OPENCODE = "%(asctime)s %(name)s %(levelname)s %(message)s"


# ═════════════════════════════════ codex ════════════════════════════════════
# (pre-refactor system-apps/codex/bin/main.py)

_codex_log = logging.getLogger("codex-sidecar")                      # :52

CODEX_CACHE_DIR  = Path("/var/lib/cseries/apps/codex/bin")             # :59
CODEX_BINARY     = CODEX_CACHE_DIR / "codex"                         # :61
CODEX_CREDS_PATH = Path("/home/privatae/.codex/credentials.json")    # :62
CODEX_AUTH_JSON_PATH = Path("/home/privatae/.codex/auth.json")       # :63

# rust-v0.135.0 → rust-v0.149.0 (2026-08-20): the 0.135.0 binary's own model
# catalog stopped at gpt-5.5 / gpt-5.3-codex; from rust-v0.146.1 the catalog
# carries gpt-5.6-sol/-terra/-luna (sol = flagship, priority 1).
# rust-v0.149.0 → rust-v0.154.0 (2026-09-17): the catalog adds gpt-6-astra
# (GPT-6-Astra, visibility=list, first row) and the CLI's own no-model default
# MOVES to it — `codex exec` with no --model banners `model: gpt-6-astra`,
# measured on THIS binary. Every gpt-5.6/5.5/5.2 row is byte-identical to the
# 0.149.0 snapshot, so astra is the only listed-row change. Re-verified on
# 0.154.0: `exec` still takes -s/--sandbox danger-full-access, -C/--cd,
# -m/--model, -c, --skip-git-repo-check and --json (JSONL), and
# `login --device-auth` still prints the two-step URL + code layout this file
# parses. The image bakes this tag (sidecars/harness-codex/Dockerfile, same
# ARG) — keep the two in step, the Dockerfile comment records what was
# verified on the binary.
# rust-v0.154.0 → rust-v0.160.1 (2026-10-06): the catalog adds gpt-6.1-sol
# (priority 1), gpt-6-sol and gpt-6-luna and drops gpt-5.2, and the CLI's own
# no-model default MOVES to gpt-6.1-sol (banner measured on THIS binary).
# `exec --help` / `login --help` are byte-identical to 0.154.0's.
CODEX_RELEASE_TAG = os.environ.get("CODEX_RELEASE_TAG", "rust-v0.160.1")  # :116
CODEX_DOWNLOAD_URL = os.environ.get(                                 # :117-121
    "CODEX_DOWNLOAD_URL",
    f"https://github.com/openai/codex/releases/download/{CODEX_RELEASE_TAG}/"
    f"codex-x86_64-unknown-linux-musl.tar.gz",
)
# The CLI's "code mode" host — a separate release asset since rust-v0.149.0,
# looked for BESIDE the codex binary at start (feature on by default; without
# it every run opens with an error frame, see the Dockerfile). Baked at build;
# this URL is only for the runtime re-acquire path.
CODEX_HOST_BINARY = CODEX_CACHE_DIR / "codex-code-mode-host"
CODEX_HOST_DOWNLOAD_URL = os.environ.get(
    "CODEX_HOST_DOWNLOAD_URL",
    f"https://github.com/openai/codex/releases/download/{CODEX_RELEASE_TAG}/"
    f"codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz",
)
CODEX_TAG_FILE = CODEX_CACHE_DIR / ".codex-release-tag"              # :126

# ── The CLI's OWN model catalog ──────────────────────────────────────────────
# `codex debug models` renders the catalog the binary ships with (the same
# table it refreshes from chatgpt.com once signed in); it runs OFFLINE in
# ~0.2 s. It is the only honest answer to "what can --model take": the coding
# matrix used to derive codex's list from the OpenRouter vendor filter, which
# still carried openai/gpt-5.3-codex after rust-v0.149.0 had dropped it — so
# a live run (2026-08-20) dispatched a model the CLI could not serve
# ("Model metadata for `gpt-5.3-codex` not found. Defaulting to fallback
# metadata"). OpenRouter's view of OpenAI is not this binary's catalog.
#
# CODEX_MODELS is the snapshot read off the PINNED binary (rust-v0.160.1,
# 2026-10-06; visibility=="list" rows only — the hidden rows are two
# unreleased `gpt-daybreak-*` rows and an internal reviewer model, all of them
# visibility=="hide"; the retired gpt-5.4 / gpt-5.4-mini rows are gone from
# the table, and gpt-5.2 is no longer in it at all). The listed gpt-5.6-* and
# gpt-5.5 rows now carry an `upgrade` hint to gpt-6-sol / gpt-6-luna; they are
# still listed, so they stay here. `efforts` is each row's
# supported_reasoning_levels verbatim — the same vendor-shipped source
# _KIMI_EFFORTS reads (support_efforts); nothing here is a guess about what
# the backend does. No listed model declares `minimal`, and the 5.6 models
# declare `max` (+ `ultra` on sol/terra) — the old flat gate had it backwards.
# gpt-6-astra declares the full ladder, `ultra` included; so do gpt-6.1-sol
# and gpt-6-sol, while gpt-6-luna stops at `max` (luna's shape since 5.6).
#
# The kernel MIRRORS this table (server/routes/inference.py CODEX_CLI_MODELS /
# CODEX_CLI_DEFAULT_MODEL) because the matrix renders without a pod;
# scripts/test_harness_effort_wiring.py gates the pairing. A CODEX_RELEASE_TAG
# bump re-reads this table from the new binary and moves both copies.
CODEX_DEFAULT_MODEL = "gpt-6.1-sol"   # `codex exec` banner with no --model —
#                                       measured on 0.160.1 (gpt-6-astra on 0.154.0,
#                                       gpt-5.6-sol on 0.149.0)
CODEX_MODELS = (
    {"id": "gpt-6-astra",   "label": "GPT-6-Astra",   "default_effort": "low",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-6.1-sol",   "label": "GPT-6.1-Sol",   "default_effort": "low",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-6-sol",     "label": "GPT-6-Sol",     "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-6-luna",    "label": "GPT-6-Luna",    "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "gpt-5.6-sol",   "label": "GPT-5.6-Sol",   "default_effort": "low",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-5.6-terra", "label": "GPT-5.6-Terra", "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh", "max", "ultra")},
    {"id": "gpt-5.6-luna",  "label": "GPT-5.6-Luna",  "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "gpt-5.5",       "label": "GPT-5.5",       "default_effort": "medium",
     "efforts": ("low", "medium", "high", "xhigh")},
)

# The PLATFORM's effort for a run that names none — per model, and the only
# thing in this file that is a platform choice rather than a reading of the
# binary. Distinct from a row's `default_effort` above, which is the catalog's
# own default_reasoning_level and DISPLAY-ONLY (nothing reads it at dispatch).
# Why it exists: with no effort passed, `codex exec` banners `reasoning
# effort: none` (measured on 0.154.0, and again on 0.160.1) — so "GPT-6-Astra
# runs at medium" had no home until this table. Decided 2026-09-17, for astra:
# every model not in this table keeps the prior behaviour of passing nothing
# and letting the vendor default stand. Since rust-v0.160.1 astra is no longer the CLI's
# no-model default (gpt-6.1-sol is), and the rule was extended to the new
# default on 2026-10-06: gpt-6.1-sol runs at medium too, so a run that names
# no model still passes medium, and a run that names gpt-6-astra still does.
# A value here still goes through the SAME per-model gate as a user's pick in
# _codex_argv, so a default the catalog stops listing is dropped and logged,
# never sent.
CODEX_DEFAULT_EFFORTS = {"gpt-6-astra": "medium", "gpt-6.1-sol": "medium"}

_CODEX_CATALOG_CACHE: dict = {"models": None, "failed_at": 0.0}
_CODEX_CATALOG_RETRY_S = 60.0


def _codex_catalog_from_binary(binary: Path | None = None,
                               timeout: float = 15.0) -> list:
    """Run `codex debug models` on the baked binary and return the LISTED rows
    in the CODEX_MODELS shape. Raises on any failure — the caller owns the
    fallback. Offline: the command renders the bundled catalog without auth."""
    import shutil
    import subprocess
    import tempfile
    binary = binary or CODEX_BINARY
    # CODEX_HOME = an empty scratch dir, so the read is OFFLINE even when a
    # grant sits in ~/.codex (on the run path derive_creds has already written
    # auth.json before the effort gate asks for the catalog): with a grant in
    # its home the CLI refreshes the catalog from chatgpt.com first — measured
    # on 0.149.0: 0.61 s with egress, a proxy-bound wait without — while an
    # empty CODEX_HOME renders the bundled table in 0.27 s with identical
    # listed rows and writes nothing there (it refuses to create helper
    # binaries under a temp dir and says so on stderr; harmless).
    scratch = tempfile.mkdtemp(prefix="codex-catalog-")
    try:
        proc = subprocess.run(
            [str(binary), "debug", "models"], capture_output=True, text=True,
            timeout=timeout,
            env={**os.environ, "HOME": "/home/privatae", "CODEX_HOME": scratch})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(f"exit {proc.returncode}: {(proc.stderr or '')[-200:]}")
    doc = json.loads(proc.stdout)
    rows = doc.get("models") if isinstance(doc, dict) else doc
    out: list = []
    for m in rows or []:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        if (m.get("visibility") or "list") != "list":
            continue
        levels = m.get("supported_reasoning_levels") or []
        efforts = tuple(str(lv.get("effort")) for lv in levels
                        if isinstance(lv, dict) and lv.get("effort"))
        out.append({"id": str(m["slug"]),
                    "label": str(m.get("display_name") or m["slug"]),
                    "default_effort": m.get("default_reasoning_level"),
                    "efforts": efforts})
    if not out:
        raise RuntimeError("catalog rendered no listed models")
    return out


def _codex_model_catalog() -> list:
    """The live catalog off the baked binary, read once per process; the pinned
    CODEX_MODELS snapshot when the binary is absent/unparseable (logged, retried
    after _CODEX_CATALOG_RETRY_S). Returns copies — callers may mutate."""
    import time as _time
    cached = _CODEX_CATALOG_CACHE.get("models")
    if cached:
        return [dict(m) for m in cached]
    now = _time.monotonic()
    if now - float(_CODEX_CATALOG_CACHE.get("failed_at") or 0.0) < _CODEX_CATALOG_RETRY_S:
        return [dict(m) for m in CODEX_MODELS]
    try:
        rows = _codex_catalog_from_binary()
    except Exception as exc:  # noqa: BLE001
        _CODEX_CATALOG_CACHE["failed_at"] = now
        _codex_log.warning("codex: `debug models` unavailable (%s) — serving the "
                           "pinned %s snapshot", exc, CODEX_RELEASE_TAG)
        return [dict(m) for m in CODEX_MODELS]
    _CODEX_CATALOG_CACHE["models"] = rows
    return [dict(m) for m in rows]


def codex_efforts_for(model) -> tuple:
    """The efforts the catalog lists for `model` (None = the CLI's default
    model). A model the catalog does not know gets the UNION of every listed
    model's efforts — since 0.149 the CLI forwards an unknown effort unchecked
    (measured: `-c model_reasoning_effort=zzz` goes on the wire), so this gate
    is the only thing keeping a typo off it and must stay a real list."""
    catalog = _codex_model_catalog()
    target = str(model or CODEX_DEFAULT_MODEL)
    for m in catalog:
        if m.get("id") == target:
            return tuple(m.get("efforts") or ())
    union: list = []
    for m in catalog:
        for e in m.get("efforts") or ():
            if e not in union:
                union.append(e)
    return tuple(union)


def _ensure_codex_auth_json(creds_path: Path = CODEX_CREDS_PATH,
                            auth_path: Path = CODEX_AUTH_JSON_PATH) -> None:
    """The codex CLI reads ~/.codex/auth.json (not credentials.json), and
    needs tokens.account_id (the chatgpt_account_id claim from the id_token).
    The user's own login surface writes the flat credentials.json; derive
    the codex-shaped auth.json from it so the CLI authenticates instead of
    looping 'Re-connecting...->401'. (was codex main.py:68-107; paths
    parameterized only so the golden test can exercise the derivation on
    temp files.)"""
    import base64
    try:
        d = json.loads(creds_path.read_text())
    except Exception:
        return
    idt = d.get("id_token") or ""
    account_id = None
    try:
        p = idt.split(".")[1]; p += "=" * (-len(p) % 4)
        account_id = (json.loads(base64.urlsafe_b64decode(p))
                      .get("https://api.openai.com/auth", {})
                      .get("chatgpt_account_id"))
    except Exception:
        pass
    auth = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": d.get("id_token"),
            "access_token": d.get("access_token"),
            "refresh_token": d.get("refresh_token"),
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc).isoformat(),
    }
    try:
        tmp = auth_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(auth, indent=2))
        tmp.chmod(0o600)
        tmp.replace(auth_path)
    except Exception as e:
        _codex_log.warning("could not write codex auth.json: %s", e)


def _codex_cache_valid() -> bool:
    # was codex main.py:149-158 — re-download if the cached binary is for a
    # different release tag, so a CODEX_RELEASE_TAG bump takes effect.
    cached_tag = ""
    try:
        cached_tag = CODEX_TAG_FILE.read_text().strip()
    except Exception:
        pass
    return (CODEX_BINARY.exists() and os.access(CODEX_BINARY, os.X_OK)
            and cached_tag == CODEX_RELEASE_TAG)


def _codex_acquire() -> None:
    # was codex main.py:159-190
    _codex_log.info("downloading codex CLI from %s", CODEX_DOWNLOAD_URL)
    tarball = CODEX_CACHE_DIR / "codex.tgz"
    with urllib.request.urlopen(CODEX_DOWNLOAD_URL, timeout=120) as r:
        tarball.write_bytes(r.read())
    with tarfile.open(tarball, "r:gz") as tf:
        # The rust-vX.Y.Z release ships the binary as a single bare file named
        # for the target triple (e.g. `codex-x86_64-unknown-linux-musl`), NOT
        # `codex`. Match the platform-suffixed name too.
        binary_member = None
        for member in tf.getmembers():
            if not member.isfile():
                continue
            base = member.name.rsplit("/", 1)[-1]
            if base == "codex" or base.startswith("codex-"):
                binary_member = member
                break
        if binary_member is None:
            raise RuntimeError("codex binary not found inside tarball")
        with tf.extractfile(binary_member) as src:  # type: ignore[union-attr]
            CODEX_BINARY.write_bytes(src.read())
        CODEX_BINARY.chmod(0o755)
    tarball.unlink(missing_ok=True)
    # The code-mode host, best-effort: the CLI runs without it (it logs one
    # error frame per run and fails code mode closed), so a failed fetch must
    # not turn a working re-acquire into a bootstrap error.
    try:
        _codex_acquire_host()
    except Exception as e:
        _codex_log.warning("codex code-mode host not acquired (%s): %s",
                           CODEX_HOST_DOWNLOAD_URL, e)
    try:
        CODEX_TAG_FILE.write_text(CODEX_RELEASE_TAG)
    except Exception:
        pass
    _codex_log.info("codex CLI %s bootstrapped at %s", CODEX_RELEASE_TAG, CODEX_BINARY)


def _codex_acquire_host() -> None:
    """Fetch the CLI's code-mode host (same release tag) to sit beside the
    binary. Same tarball shape as the CLI's own: one bare file named for the
    target triple."""
    tarball = CODEX_CACHE_DIR / "codex-host.tgz"
    with urllib.request.urlopen(CODEX_HOST_DOWNLOAD_URL, timeout=120) as r:
        tarball.write_bytes(r.read())
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            member = None
            for m in tf.getmembers():
                if m.isfile() and m.name.rsplit("/", 1)[-1].startswith("codex-code-mode-host"):
                    member = m
                    break
            if member is None:
                raise RuntimeError("codex-code-mode-host not found inside tarball")
            with tf.extractfile(member) as src:  # type: ignore[union-attr]
                CODEX_HOST_BINARY.write_bytes(src.read())
            CODEX_HOST_BINARY.chmod(0o755)
    finally:
        tarball.unlink(missing_ok=True)
    _codex_log.info("codex code-mode host %s at %s", CODEX_RELEASE_TAG, CODEX_HOST_BINARY)


# Reasoning efforts for `model_reasoning_effort` — PER MODEL, from the CLI's
# own catalog (CODEX_MODELS above / `codex debug models` live). History: the
# rust-v0.135.0 binary hard-exited on an out-of-set value ("unknown variant
# `zzz`, expected one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`")
# and that flat list was the gate. rust-v0.149.0 no longer rejects anything —
# `-c model_reasoning_effort=zzz` goes on the wire (measured 2026-08-20) — so
# the gate is now the only thing keeping a typo off it, and the list it checks
# is the catalog's supported_reasoning_levels for the model being run (the
# same vendor-shipped source _KIMI_EFFORTS reads). `none` is deliberately not
# offered: it means "no reasoning", a model choice rather than an effort level.
# _CODEX_EFFORTS is the UNION across listed models — what the kernel's picker
# offers (HARNESS_EFFORTS["codex"], mirrored; the effort-wiring gate pairs
# them) — while codex_efforts_for(model) is what a run actually accepts.
_CODEX_EFFORTS = tuple(
    e for e in ("low", "medium", "high", "xhigh", "max", "ultra")
    if any(e in m["efforts"] for m in CODEX_MODELS))


def _codex_argv(spec, prompt, project_dir, model, effort=None) -> list:
    # was codex main.py:300-304 — `exec` is the non-interactive one-shot mode.
    # `--sandbox danger-full-access`: codex's internal bubblewrap sandbox can't
    # run in the pod (no bwrap; seccomp blocks unshare) — the OUTER environment
    # is the sandbox. `--skip-git-repo-check`: the worktree may not be a repo.
    cmd = [str(spec.binary), "exec", "--sandbox", "danger-full-access",
           "--skip-git-repo-check", "--cd", project_dir]
    if model:
        cmd.extend(["--model", model])
    # Effort rides as a config override — codex has no --reasoning-effort flag;
    # the knob is `model_reasoning_effort` in config.toml, and -c overrides it
    # per run. A value the catalog does not list FOR THIS MODEL is DROPPED with
    # a log rather than passed: 0.149 forwards anything, so a typo or a
    # cross-model pick (ultra on gpt-5.5) would otherwise reach the vendor
    # unchecked (the _KIMI_EFFORTS contract, now per model).
    eff = str(effort or "").strip().lower()
    if not eff:
        # No effort named: the platform's per-model default (CODEX_DEFAULT_EFFORTS)
        # if there is one, else nothing — the vendor's own default stands.
        eff = CODEX_DEFAULT_EFFORTS.get(str(model or CODEX_DEFAULT_MODEL), "")
    if eff:
        accepted = codex_efforts_for(model)
        if eff in accepted:
            cmd.extend(["-c", f"model_reasoning_effort={eff}"])
        else:
            _codex_log.info("codex: dropping effort %r — %s accepts %s",
                            eff, model or CODEX_DEFAULT_MODEL,
                            ", ".join(accepted) or "(no efforts listed)")
    # The host fetch channel (host_mcp): `-c mcp_servers.host.*` dotted-path
    # overrides, nothing written to ~/.codex/config.toml. Empty when the
    # operator injected no channel, so the argv is byte-identical to before.
    cmd.extend(host_mcp.codex_config_overrides())
    cmd.append(prompt)
    return cmd


def _codex_env(spec, creds) -> dict:
    # was codex main.py:312 — auth flows through ~/.codex/auth.json, not env.
    return {**os.environ, "HOME": "/home/privatae"}


# The vendor's OWN login. VERIFIED on the pinned binary (rust-v0.135.0):
# `codex login --device-auth` is a real flag on the login subcommand. Device
# auth prints a URL and blocks until the user finishes in a browser — the
# platform never touches the grant.
# RE-VERIFIED on rust-v0.149.0 (2026-08-20): `codex login --help` no longer
# LISTS --device-auth, but the flag is still accepted (present in the binary,
# and running it prints the same two-step URL + code layout below, measured).
# RE-VERIFIED again on rust-v0.154.0 (2026-09-17), same finding: `login --help`
# lists only `status` + --with-api-key, and --device-auth still runs and still
# prints the two-step layout the parser below reads.
# RE-VERIFIED on rust-v0.160.1 (2026-10-06): `login --help` is byte-identical
# to 0.154.0's, and --device-auth prints the same layout with one caution line
# after the code ("Continue only if you started this login in Codex. …"); the
# URL is still https://auth.openai.com/codex/device and the code regex below
# reads the code (measured, signed out).
def _codex_login_argv(spec) -> list:
    return [str(spec.binary), "login", "--device-auth"]


# What `codex login --device-auth` prints (rust-v0.135.0, run against the
# pinned binary 2026-08-20; rust-v0.149.0 prints the identical layout with
# "Welcome to Codex [v0.149.0]" above it — measured the same day; ANSI stripped):
#
#   1. Open this link in your browser and sign in to your account
#      https://auth.openai.com/codex/device
#
#   2. Enter this one-time code (expires in 15 minutes)
#      2N41-F3AX9
#
# The code is on its OWN line, three lines after the URL. A reader that stops
# at the URL (harness_core.make_login before 2026-08-20) hands the user a link
# and nothing to type on it — measured on a live Host the same day. Group 1 is
# the code; the vendor's codes have been 4 chars, a dash, 5 chars — the
# bounds below are loose on purpose (the regex only runs on lines AFTER the
# URL, so the version banner and the URL itself can never match).
_CODEX_LOGIN_CODE_RE = r"\b([A-Z0-9]{4,8}-[A-Z0-9]{4,8})\b"


def _codex_login_export(spec) -> dict:
    """The grant the vendor's login wrote, in the shape the ENTITY stores.

    `codex login` writes ~/.codex/auth.json — {"tokens": {id_token,
    access_token, refresh_token, account_id}, "last_refresh", ...} — but the
    entity stores codex as the FLAT credentials.json (server/routes/review.py
    _HARNESS_CRED_FILES / harnesses.PROVIDERS creds_path / harness_keys
    _PRIMARY_CREDS all name that one file), and _ensure_codex_auth_json above
    re-derives auth.json from it at every wake. So export the flat shape: one
    stored shape, one derivation, and the Keys-app paste path and the vendor
    login path land in the same file. A flat auth.json (no "tokens" wrapper)
    is accepted too. {} when there is no usable access_token — the caller
    treats that as "no grant landed", never persists an empty file."""
    path = spec.alt_auth_path
    if path is None:
        return {}
    try:
        d = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    toks = d.get("tokens") if isinstance(d.get("tokens"), dict) else d
    flat = {k: toks.get(k) for k in ("id_token", "access_token", "refresh_token")
            if isinstance(toks.get(k), str) and toks.get(k).strip()}
    if not flat.get("access_token"):
        return {}
    flat["token_type"] = "Bearer"
    return {"credentials.json": json.dumps(flat, indent=2)}


CODEX = HarnessSpec(
    id="codex", display="codex", logger=_codex_log, log_format=LOG_FMT,
    login_argv=_codex_login_argv,
    login_code_re=_CODEX_LOGIN_CODE_RE,
    login_export=_codex_login_export,
    binary=CODEX_BINARY, workspace=WORKSPACE, cache_dir=CODEX_CACHE_DIR,   # :59-65
    baked=False, cache_valid=_codex_cache_valid, acquire=_codex_acquire,
    release_tag=lambda: CODEX_RELEASE_TAG,                                 # :250
    creds_path=CODEX_CREDS_PATH, creds_shape="oauth_json",                 # :198-216
    # The VENDOR login (login_argv above) writes the CLI's own session file,
    # ~/.codex/auth.json — never credentials.json, which is only ever produced
    # by the user's paste-a-key surface (and by _ensure_codex_auth_json in the
    # other direction). Without this path set, harness_core._creds_state read
    # credentials.json alone and reported not-connected straight after a
    # SUCCESSFUL `codex login`, so run_auth_ok gated /run to 401 on a live
    # grant. grok has carried the same dual deployment-key/OAuth-session state
    # all along (:516); codex needed it and never got it.
    alt_auth_path=CODEX_AUTH_JSON_PATH,                                    # :63
    auth_error=("codex not connected — connect it on your own surface; "
                "the platform ships no sign-in"),                         # :271
    build_argv=_codex_argv, build_env=_codex_env,
    run_cwd=False,                                                         # :307-313 (no cwd; --cd)
    timeout_error=lambda m: f"codex exceeded {m}min cap",                  # :328
    # :245-251 kept the two original fields first; default_model + models
    # (2026-08-20) follow so /status states what a run with no --model gets
    # and what --model can take — off THIS binary, not a reseller's catalog.
    status_tail=("bootstrap_error", "release_tag", "default_model", "models"),
    default_model=CODEX_DEFAULT_MODEL,
    models=_codex_model_catalog,
    derive_creds=_ensure_codex_auth_json,                                  # :274
    run_auth_ok=lambda spec, st: bool(st["valid"]),                        # :269-271
    default_max_minutes=int(os.environ.get("CODEX_DEFAULT_MAX_MINUTES", "10")),  # :130
)


# ═════════════════════════════════ cursor ═══════════════════════════════════
# (pre-refactor system-apps/cursor/bin/main.py)

_cursor_log = logging.getLogger("cursor-sidecar")                    # :38

CURSOR_CACHE_DIR = Path("/var/lib/cseries/apps/cursor/bin")            # :43
# cursor ships a whole dist-package/ tree (launcher + bundled node + *.node +
# rg + sandbox); the launcher cannot run without its siblings (:45-50).
CURSOR_BIN = CURSOR_CACHE_DIR / "dist-package" / "cursor-agent"      # :50
CURSOR_CREDS_PATH = Path("/home/privatae/.cursor/api_key")           # :51

CURSOR_RELEASE_TAG = os.environ.get("CURSOR_RELEASE_TAG", "2026.10.01-e373342")  # :60
CURSOR_DOWNLOAD_URL = os.environ.get(                                # :61-65
    "CURSOR_DOWNLOAD_URL",
    f"https://downloads.cursor.com/lab/{CURSOR_RELEASE_TAG}/linux/x64/"
    f"agent-cli-package.tar.gz",
)


def _cursor_cache_valid() -> bool:
    # was cursor main.py:82-84
    return CURSOR_BIN.exists() and os.access(CURSOR_BIN, os.X_OK)


def _cursor_acquire() -> None:
    # was cursor main.py:85-115
    _cursor_log.info("downloading cursor-agent from %s", CURSOR_DOWNLOAD_URL)
    tarball = CURSOR_CACHE_DIR / "cursor-agent.tgz"
    # downloads.cursor.com 403s the default Python-urllib User-Agent; send a
    # browser UA so the CDN serves the tarball. (:88-94)
    _req = urllib.request.Request(
        CURSOR_DOWNLOAD_URL,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
    )
    with urllib.request.urlopen(_req, timeout=120) as r:
        tarball.write_bytes(r.read())
    with tarfile.open(tarball, "r:gz") as tf:
        # Extract the FULL dist-package/ tree; filter='data' blocks path
        # traversal / absolute paths. (:97-102)
        tf.extractall(CURSOR_CACHE_DIR, filter="data")
    if not CURSOR_BIN.exists():
        raise RuntimeError("cursor-agent not found in extracted dist-package")
    # Make the launcher + bundled native helpers executable. (:105-111)
    for p in CURSOR_BIN.parent.iterdir():
        try:
            if p.is_file():
                p.chmod(0o755)
        except OSError:
            pass
    tarball.unlink(missing_ok=True)
    _cursor_log.info("cursor-agent bootstrapped at %s", CURSOR_BIN)


# ── The CLI's OWN model catalog ──────────────────────────────────────────────
# `cursor-agent --list-models`. The contract below was READ OFF THE PINNED
# PACKAGE'S OWN SOURCE (2026.05.28-a70ca7c: dist-package/730.index.js =
# "./src/commands/models.ts", plus "./src/console-io.ts" and 8262.index.js in
# index.js) — not from docs, and not from a hopeful parse of one sample.
# Re-read at 2026.10.01-e373342 (models.ts is now 6436.index.js, the "Cannot
# use this model" path 1623.index.js, console-io still index.js): same header,
# row layout, marks, stdout/stderr split and refusal field; the Tip line is
# longer and still starts "Tip:". The one change is in the unusable-key bullet:
#
#   * It is an ACCOUNT read. handleModelsList builds a backend client and awaits
#     model-service -> {availableModels, defaultModel}; with no grant it prints
#     "No models available for this account." So the list is what the USER'S OWN
#     Cursor plan serves, and there is deliberately NO baked snapshot here the
#     way codex has CODEX_MODELS — a baked list would be this image asserting
#     what somebody else's plan includes. (codex can bake one because
#     `codex debug models` renders a catalog the binary SHIPS, offline.)
#   * Success goes to STDOUT and exits 0 (console-io.OT -> process.stdout.write);
#     failure prints "Failed to load models: <msg>" to STDERR and exits 1
#     (console-io.p2 -> process.stderr.write).
#   * An UNUSABLE key was NOT that failure path on 2026.05.28: measured
#     2026-08-21 with both no key and a bogus one, exit 0, empty stderr, and
#     the same "No models available for this account." a genuinely empty plan
#     prints. 2026.10.01-e373342 changed that: its handler (6436.index.js)
#     exits 1 when the usable-models read fails ("Authentication failed: …"
#     for an auth error, else "Failed to load models: …"), and with no
#     credential at all exits 1 with "Error: Authentication required. …"
#     (measured signed-out 2026-10-06; a bogus key not measured). This file
#     turns either failure into [], so an empty list still reaches the
#     entity, which deliberately refuses to cache one
#     (review._maybe_cache_model_catalog) rather than freeze the picker at
#     "none" after the user fixes their key.
#   * The printed layout is the printer's own:
#         Available models                       <- dim header
#         <blank>
#         <displayModelId||modelId>[ - <displayName>][ (current[, default])]
#         ...
#         <blank>
#         Tip: use --model <id> (or /model <id> in interactive mode) to switch.
#     chalk colours each id, so ANSI has to come off before parsing
#     (harness_core._strip_ansi:543).
#   * The printed id is `displayModelId || modelId`, and that is EXACTLY what
#     --model accepts: the CLI's own refusal path prints "Cannot use this model:
#     <X>. Available models: <availableModels.map(displayModelId)>" — the same
#     field. So parsing this output yields ids that dial, by construction.
#
# WHY THIS EXISTS: until 2026-08-21 the coding matrix derived cursor's model
# list from the OpenRouter catalog filtered by vendor
# (inference.HARNESS_MODEL_VENDORS["cursor"] = openai/anthropic/google/x-ai).
# OpenRouter's view of a vendor is not Cursor's account catalog, and on a
# 7-harness fan-out (2026-08-21) that mismatch
# dispatched `gpt-5.6-luna-pro` — a real OpenRouter slug Cursor has never
# served. The CLI refused it in 7 s with the "Cannot use this model" line above.
# Same class as the codex phantom-model bug of 2026-08-20,
# and the same fix: ask the CLI what it can run.
_CURSOR_CATALOG_CACHE: dict = {"models": None, "default": None, "failed_at": 0.0}
_CURSOR_CATALOG_RETRY_S = 60.0
_CURSOR_HEADER = "Available models"
_CURSOR_TIP_PREFIX = "Tip:"
_CURSOR_NO_MODELS = "No models available for this account."
# The only two words the printer ever puts in that trailing parenthesis.
_CURSOR_MARKS = ("current", "default")


def _cursor_parse_models(stdout: str) -> tuple:
    """(models, default_id) from `--list-models` output. models = [{id, label?,
    default?}] in the vendor's own order. An empty list is a real answer (a
    signed-in account whose plan serves nothing), never an error."""
    from .harness_core import _strip_ansi
    models: list = []
    default_id = None
    for raw in _strip_ansi(stdout or "").splitlines():
        line = raw.strip()
        if not line or line == _CURSOR_HEADER or line.startswith(_CURSOR_TIP_PREFIX):
            continue
        if line == _CURSOR_NO_MODELS:
            return [], None
        marks: list = []
        if line.endswith(")"):
            head, sep, tail = line.rpartition(" (")
            # Only strip a trailing "(...)" that IS the marker set — a display
            # name may legitimately end in a parenthesis ("Claude Opus 5
            # (Thinking)"). A name ending in the literal " (default)" would
            # still be read as the marker; the ID stays correct either way, and
            # the vendor prints those two words itself, so the residue is a
            # cosmetic label/flag, never a wrong model id.
            if sep and all(t.strip() in _CURSOR_MARKS for t in tail[:-1].split(",")):
                line, marks = head.rstrip(), [t.strip() for t in tail[:-1].split(",")]
        mid, _, label = line.partition(" - ")
        mid = mid.strip()
        if not mid:
            continue
        row: dict = {"id": mid}
        if label.strip():
            row["label"] = label.strip()
        if "default" in marks:
            row["default"] = True
            default_id = mid
        models.append(row)
    return models, default_id


def _cursor_catalog_from_binary(binary=None, timeout: float = 45.0) -> tuple:
    """(models, default_id) off the pinned binary. Raises on any failure — the
    caller owns the fallback. Needs the account's key: this is a backend read,
    not an offline table."""
    import subprocess
    from .harness_core import _strip_ansi
    binary = binary or CURSOR_BIN
    creds = _read_creds(CURSOR)
    if not creds:
        raise RuntimeError("cursor is not connected — no API key to list models with")
    proc = subprocess.run(
        [str(binary), "--list-models"], capture_output=True, text=True,
        timeout=timeout,
        env={**os.environ, "HOME": "/home/privatae", "CURSOR_API_KEY": str(creds)})
    if proc.returncode != 0:
        raise RuntimeError(
            f"exit {proc.returncode}: {_strip_ansi(proc.stderr or '')[-200:]}")
    return _cursor_parse_models(proc.stdout)


def _cursor_model_catalog() -> list:
    """The ACCOUNT's catalog, read once per process (a failure is retried after
    _CURSOR_CATALOG_RETRY_S). [] when cursor is not connected or the vendor read
    failed — never a fabricated list, for the reason in the header. The entity
    caches these rows when it reads GET /status."""
    import time as _time
    cached = _CURSOR_CATALOG_CACHE.get("models")
    if cached is not None:
        return [dict(m) for m in cached]
    now = _time.monotonic()
    if now - float(_CURSOR_CATALOG_CACHE.get("failed_at") or 0.0) < _CURSOR_CATALOG_RETRY_S:
        return []
    try:
        rows, default_id = _cursor_catalog_from_binary()
    except Exception as exc:  # noqa: BLE001
        _CURSOR_CATALOG_CACHE["failed_at"] = now
        _cursor_log.warning("cursor: `--list-models` unavailable (%s)", exc)
        return []
    _CURSOR_CATALOG_CACHE["models"] = rows
    _CURSOR_CATALOG_CACHE["default"] = default_id
    return [dict(m) for m in rows]


def _cursor_argv(spec, prompt, project_dir, model, effort=None) -> list:
    # was cursor main.py:208-216 — -p/--print = non-interactive with full
    # write+shell tool access; --force allows commands; prompt is POSITIONAL;
    # no --cwd flag exists, the agent works in the process cwd (hence
    # run_cwd=True below).
    cmd = [str(spec.binary), "-p", "--force", "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    return cmd


def _cursor_env(spec, creds) -> dict:
    # was cursor main.py:225
    return {**os.environ, "CURSOR_API_KEY": creds, "HOME": "/home/privatae"}


CURSOR = HarnessSpec(
    id="cursor", display="cursor-agent", logger=_cursor_log, log_format=LOG_FMT,
    binary=CURSOR_BIN, workspace=WORKSPACE, cache_dir=CURSOR_CACHE_DIR,    # :43-53
    baked=False, cache_valid=_cursor_cache_valid, acquire=_cursor_acquire,
    release_tag=lambda: CURSOR_RELEASE_TAG,                                # :179
    creds_path=CURSOR_CREDS_PATH, creds_shape="json_then_raw",             # :123-139
    auth_error="Cursor API key not set — add it in the Keys app",          # :193
    build_argv=_cursor_argv, build_env=_cursor_env,
    run_cwd=True,                                                          # :224
    timeout_error=lambda m: f"cursor-agent exceeded {m}min cap",           # :238
    # `models` (2026-08-21) so /status carries what --model can actually take
    # for THIS account. No `default_model`: cursor's default is per-account too
    # (the catalog marks it) and status_tail emits that field as a static
    # attribute, so the honest place for it is the `default: true` row.
    status_tail=("bootstrap_error", "release_tag", "models"),               # :174-180
    models=_cursor_model_catalog,
    default_max_minutes=int(os.environ.get("CURSOR_DEFAULT_MAX_MINUTES", "10")),  # :67
)


# ══════════════════════════════════ grok ════════════════════════════════════
# (pre-refactor system-apps/grok/bin/main.py)

_grok_log = logging.getLogger("grok-sidecar")                        # :40

GROK_CACHE_DIR = Path("/var/lib/cseries/apps/grok/bin")                # :47
GROK_BIN = GROK_CACHE_DIR / "grok"                                   # :49
# `--reasoning-effort <LEVEL>` (alias `--effort`), READ OFF THE PINNED BINARY
# (0.2.112, extracted from cseries-harness-grok:v0.2.7 and run/strung 2026-08-28),
# not off the flag name. Its own embedded docs: "Canonical levels: `none`,
# `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (each a distinct tier; a
# model only accepts the levels its menu advertises)", and "`--reasoning-effort`
# /`--effort` ... work in both modes" — i.e. headless `-p` too, which is the
# only mode we run. `--help` does NOT enumerate the values (no clap enum), so
# they come from that shipped text, the same vendor-supplied source
# _KIMI_EFFORTS and CODEX_MODELS read. Re-read off 1.0.46 (2026-10-06): the
# same seven canonical levels, word for word.
#
# A level this tuple does not list is DROPPED with a log rather than forwarded —
# the _KIMI_EFFORTS/codex contract. Grok also accepts per-model menu option ids
# (its docs name `deep`), which this deliberately does NOT pass: inventing a
# client-side list of vendor ids is the cursor-catalog bug, and the canonical
# tiers are what the platform's own effort vocabulary offers anyway.
#
# Passing a canonical level the ACTIVE MODEL happens not to support is safe and
# does not need a per-model table here: measured in the same binary, the CLI
# warns "--effort/--reasoning-effort: model does not support reasoning effort;
# ignoring" and continues. Mirrored by inference.HARNESS_EFFORTS["grok"] (minus
# `none`/`minimal`, which the picker does not offer — the codex rule); the
# effort-wiring gate pairs them.
GROK_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Two headless auth modes (:50-56): api_key = GROK_DEPLOYMENT_KEY paste;
# auth.json = the grok CLI `login` OAuth session (grok reads it itself).
GROK_CREDS_PATH = Path("/home/privatae/.grok/api_key")               # :55
GROK_AUTH_JSON_PATH = Path("/home/privatae/.grok/auth.json")         # :56

GROK_CHANNEL_URL = os.environ.get("GROK_CHANNEL_URL", "https://x.ai/cli/stable")  # :63-66
GROK_BIN_URL_PRIMARY = "https://x.ai/cli"                            # :69
GROK_BIN_URL_FALLBACK = "https://storage.googleapis.com/grok-build-public-artifacts/cli"  # :70

# The original mutated module globals GROK_RELEASE_TAG (:67, set by
# _resolve_channel_head :83-93) and _resolved_version (:80, set by bootstrap);
# one holder replaces both.
_grok_channel = {
    "release_tag": os.environ.get("GROK_RELEASE_TAG"),  # None = auto-resolve (:67)
    "resolved_version": None,                           # (:80)
}


def _grok_resolve_channel_head() -> str:
    # was grok main.py:83-93 — fetch the current stable version from the
    # channel pointer; GROK_RELEASE_TAG pins it.
    if _grok_channel["release_tag"]:
        return _grok_channel["release_tag"]
    with urllib.request.urlopen(GROK_CHANNEL_URL, timeout=30) as r:
        version = r.read().decode().strip()
    if not version:
        raise RuntimeError("channel head returned empty body")
    _grok_channel["release_tag"] = version
    return version


def _grok_cache_valid() -> bool:
    # was grok main.py:106-108
    return GROK_BIN.exists() and os.access(GROK_BIN, os.X_OK)


def _grok_acquire() -> None:
    # was grok main.py:109-127 — raw single-file binary (NOT a tarball),
    # primary x.ai URL with a GCS mirror fallback.
    version = _grok_resolve_channel_head()
    _grok_channel["resolved_version"] = version
    asset = f"grok-{version}-linux-x86_64"
    tried = []
    for base in (GROK_BIN_URL_PRIMARY, GROK_BIN_URL_FALLBACK):
        url = f"{base}/{asset}"
        tried.append(url)
        try:
            _grok_log.info("downloading grok %s from %s", version, url)
            with urllib.request.urlopen(url, timeout=120) as r:
                GROK_BIN.write_bytes(r.read())
            GROK_BIN.chmod(0o755)
            _grok_log.info("grok CLI bootstrapped at %s (v%s)", GROK_BIN, version)
            return
        except Exception as e:
            _grok_log.warning("grok download from %s failed: %s", url, e)
    raise RuntimeError(f"all download targets failed: {tried}")


def _grok_argv(spec, prompt, project_dir, model, effort=None) -> list:
    # was grok main.py:239-249 — `-p/--single` is the headless one-shot prompt;
    # `--always-approve` auto-approves tool use (so it can WRITE files);
    # `--cwd` scopes it to the workspace. GROK_RUN_FLAG overrides the flag
    # without a rebuild — read per call, as the original did (:246). NOTE the
    # original appended --model AFTER the prompt; preserved.
    grok_flag = os.environ.get("GROK_RUN_FLAG", "-p")
    cmd = [str(spec.binary), "--cwd", project_dir, "--always-approve", grok_flag, prompt]
    if model:
        cmd.extend(["--model", model])
    # Effort was accepted and silently discarded here until 2026-08-28 — the
    # binary has had `--reasoning-effort` all along (see GROK_EFFORTS), so this
    # was a real capability sitting unusable, which is the exact case
    # scripts/test_harness_effort_wiring.py exists to catch. It stayed green
    # because BOTH sides were absent: no read here, no HARNESS_EFFORTS["grok"].
    # The flag goes after --model, matching how the original appended --model
    # after the prompt (grok parses flags following -p's value).
    eff = str(effort or "").strip().lower()
    if eff:
        if eff in GROK_EFFORTS:
            cmd.extend(["--reasoning-effort", eff])
        else:
            _grok_log.info("grok: dropping effort %r — accepts %s",
                           eff, ", ".join(GROK_EFFORTS))
    return cmd


def _grok_env(spec, creds) -> dict:
    # was grok main.py:253-257 — deployment-key auth injects
    # GROK_DEPLOYMENT_KEY; OAuth (auth.json) is read by grok itself, so don't
    # override it with an empty key.
    env = {**os.environ, "HOME": "/home/privatae"}
    if creds:
        env["GROK_DEPLOYMENT_KEY"] = creds
    return env


# The vendor's OWN login. `grok login --device-auth` is the command this
# platform already drove as a subprocess in the (now deleted) connect flow —
# see the grok_cli_subprocess note in server/routes/harnesses.py. Carried over
# from that integration rather than re-verified against the binary: grok is
# not baked, so its binary only exists after a pod bootstrap.
def _grok_login_argv(spec) -> list:
    return [str(spec.binary), "login", "--device-auth"]


GROK = HarnessSpec(
    id="grok", display="grok", logger=_grok_log, log_format=LOG_FMT,
    login_argv=_grok_login_argv,
    binary=GROK_BIN, workspace=WORKSPACE, cache_dir=GROK_CACHE_DIR,        # :47-58
    baked=False, cache_valid=_grok_cache_valid, acquire=_grok_acquire,
    # Peer contract field: the resolved channel head, or GROK_RELEASE_TAG if
    # pinned (:200-205).
    release_tag=lambda: _grok_channel["release_tag"] or _grok_channel["resolved_version"],
    creds_path=GROK_CREDS_PATH, creds_shape="raw_or_json_envelope",        # :136-151
    creds_fields=("api_key", "token"),                                     # :147
    alt_auth_path=GROK_AUTH_JSON_PATH,                                     # :154-160
    auth_error=("grok not connected — provide a GROK_DEPLOYMENT_KEY "
                "(console.x.ai) in the Keys app, or run `grok login`"),    # :220-221
    build_argv=_grok_argv, build_env=_grok_env,
    run_cwd=True,                                                          # :262
    timeout_error=lambda m: f"grok run timed out after {m}m",              # :276
    status_tail=("release_tag", "bootstrap_error"),                        # :198-207 (swapped order)
    run_auth_ok=lambda spec, tok: bool(tok) or alt_oauth_present(spec.alt_auth_path),  # :218-221
    catch_run_errors=False,  # :251-276 — no generic except in the original /run
    default_max_minutes=int(os.environ.get("GROK_DEFAULT_MAX_MINUTES", "10")),  # :72
)


# ════════════════════════════════ opencode ══════════════════════════════════
# (pre-refactor system-apps/opencode/bin/main.py)

_opencode_log = logging.getLogger("opencode-sidecar")                # :27

OPENCODE_CACHE_DIR = Path("/var/lib/cseries/apps/opencode/bin")        # :31
OPENCODE_BINARY = OPENCODE_CACHE_DIR / "opencode"                    # :32
OPENCODE_CREDS_PATH = Path(os.environ.get("OPENCODE_API_KEY_PATH",   # :36-37
                                          "/home/privatae/.opencode/api_key"))
# opencode runs the model the user configured for it in the Orchestrator
# Coding section (the dispatcher passes it per-run in the /run body). There is
# NO compile-time default model (no-pins doctrine): the env
# var is a deployment-level override, and empty means unconfigured — the run
# errors instead of guessing a model. (:40-41)
OPENCODE_DEFAULT_MODEL = os.environ.get("OPENCODE_DEFAULT_MODEL", "")


def _opencode_normalize_model(raw) -> str:
    # was opencode main.py:99-104 — bare slugs route through OpenRouter: the
    # key we hold is an OpenRouter key, so an unprefixed model can only mean
    # that provider. No model at all is UNCONFIGURED, not a default: the run
    # must name the model picked in the Orchestrator Coding section, and the
    # error propagates (make_run normalizes OUTSIDE its try).
    model = (raw or "").strip() or OPENCODE_DEFAULT_MODEL
    if not model:
        raise ValueError(
            "opencode has no model configured — pick one in the Orchestrator "
            "Coding section")
    if "/" in model and not model.startswith("openrouter/"):
        model = f"openrouter/{model}"
    return model


def _opencode_argv(spec, prompt, project_dir, model, effort=None) -> list:
    # was opencode main.py:105-109 — non-interactive one-shot; model is always
    # set (normalize_model errors on unconfigured before argv is built). The
    # OUTER environment is the sandbox, same doctrine as codex's disabled
    # internal sandbox.
    return [str(spec.binary), "run", "--model", model, prompt]


def _opencode_env(spec, creds) -> dict:
    # was opencode main.py:118-119 — opencode reads provider keys from env.
    return {**os.environ, "HOME": "/home/privatae", "OPENROUTER_API_KEY": creds}


# ── opencode's output-token ceiling ──────────────────────────────────────────
# opencode caps every model step's output at its own OUTPUT_TOKEN_MAX, not the
# model's. Read off the pinned v1.18.35 binary:
#   var V7=32000                                   (OUTPUT_TOKEN_MAX)
#   function yy($,Z=V7){return Math.min($.limit.output,Z)||Z}
#   maxOutputTokens: ue.maxOutputTokens(e.model, e.flags.outputTokenMax)
#   outputTokenMax: G("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX")
# so a step gets min(model's limit, 32000). Run 742 (2026-10-07) ran
# z-ai/glm-5.3-prime, whose catalog limit is 131,072, and stopped "length" at
# exactly 32,000 (31,998 reasoning + 2 output) with nothing written.
#
# The override cannot simply be set high for every run. A model whose catalog
# limit is UNKNOWN (limit.output 0 — 225 entries in that binary's catalog)
# gets min(0, X) || X = X: the override itself is sent as the request's
# maxOutputTokens, and the compaction budget (context - maxOutputTokens,
# clamped at 0) collapses for the 81 of those whose context is known. So the
# override is set PER RUN to the model's own limit as opencode itself resolves
# it (`opencode models <provider> --verbose` prints each model's JSON, limit
# included), and left unset when that limit is unknown or cannot be read —
# opencode's own 32,000 default, exactly as before, for that case only.
#
# The override also sets opencode's COMPACTION point. The same binary:
#   function vn(e){let o=e.model.limit.context;if(o===0)return 0;
#     let l=e.cfg.compaction?.reserved??Math.min(Ud,maxOutputTokens(...));
#     return e.model.limit.input?Math.max(0,e.model.limit.input-l)
#                               :Math.max(0,o-maxOutputTokens(...))}
# and it compacts once a step's tokens reach vn(). For a model with no
# limit.input, every token of output ceiling is a token less of input before
# compaction, and a catalog output limit equal to the context leaves NONE
# (qwen/qwen3.6-27b 262,144 / 262,140, openrouter/auto, bodybuilder in that
# binary's openrouter catalog: compaction after every step). So for those
# models the ceiling is at most half the context — never below opencode's
# own 32,000, which is what it ran at before. A model with limit.input
# reserves at most 20,000 (Ud) whatever the ceiling, and keeps its own.
OPENCODE_OUTPUT_MAX_ENV = "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"
_OC_DEFAULT_OUTPUT_MAX = 32000          # V7 in v1.18.35: opencode's own ceiling
_OC_LIMIT_TIMEOUT_S = 20
# model -> limit.output (0 = unknown). Per pod process: the catalog is the
# pinned binary's, so it does not change under a running sidecar.
_OC_LIMITS: dict = {}


def _opencode_catalog_limit(binary, model: str, env: dict) -> int:
    """The model's limit.output as `opencode models` states it; 0 when the
    catalog has no limit for it or the listing could not be read."""
    if model in _OC_LIMITS:
        return _OC_LIMITS[model]
    provider, sep, _rest = model.partition("/")
    if not sep:
        return 0
    import subprocess
    try:
        r = subprocess.run([str(binary), "models", provider, "--verbose"],
                           env=env, capture_output=True, text=True,
                           timeout=_OC_LIMIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        _opencode_log.warning("opencode models %s failed: %s", provider, exc)
        return 0
    lines = (r.stdout or "").splitlines()
    limit = 0
    for i, ln in enumerate(lines):
        if ln.strip() != model:
            continue
        # JSON.stringify(model, null, 2): the object opens on the next line
        # and closes on the first line that is exactly "}".
        block = []
        for nxt in lines[i + 1:]:
            block.append(nxt)
            if nxt == "}":
                break
        def _pos(v) -> int:
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0
        try:
            obj = json.loads("\n".join(block))
            lim = obj.get("limit") or {}
            limit = _pos(lim.get("output"))
            ctx = _pos(lim.get("context"))
            if limit and ctx and not _pos(lim.get("input")):
                # Keep at least half the context for input (see vn above).
                limit = min(limit, max(_OC_DEFAULT_OUTPUT_MAX, ctx // 2))
        except (ValueError, TypeError, AttributeError):
            limit = 0
        break
    if r.returncode == 0:
        _OC_LIMITS[model] = limit
    return limit


def _opencode_run_env(spec, model, env) -> dict:
    """{OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX: <the model's own limit>} when
    the catalog knows it, else {} (see the block above). A deployment that
    set the variable itself keeps its value."""
    if not model or OPENCODE_OUTPUT_MAX_ENV in (env or {}):
        return {}
    limit = _opencode_catalog_limit(spec.binary, str(model), env)
    return {OPENCODE_OUTPUT_MAX_ENV: str(limit)} if limit > 0 else {}


OPENCODE = HarnessSpec(
    id="opencode", display="opencode", logger=_opencode_log,
    log_format=LOG_FMT_OPENCODE,
    binary=OPENCODE_BINARY, workspace=WORKSPACE,
    cache_dir=None,   # the original never mkdir'd its cache dir (:31-34)
    # Binary BAKED into the image at build (sidecars/harness-opencode/
    # Dockerfile); no runtime download, no bootstrap 503s in /run (:12-14).
    baked=True, cache_valid=None, acquire=None, release_tag=None,
    creds_path=OPENCODE_CREDS_PATH, creds_shape="plaintext_or_empty",      # :45-49
    auth_error=("opencode not connected — add your OpenRouter API key "
                "in the Keys app"),                                        # :85-86
    build_argv=_opencode_argv, build_env=_opencode_env,
    run_env=_opencode_run_env,
    run_cwd=True,                                                          # :117
    timeout_error=lambda m: f"opencode exceeded {m}min cap",               # :134
    status_tail=("default_model",),                                        # :66-75
    normalize_model=_opencode_normalize_model,                             # :99-104
    report_model=lambda m: m,   # :127 — always the normalized model, never "default"
    # /status surfaces the deployment override when one is set; None =
    # unconfigured (per-run model comes from the Orchestrator Coding section).
    default_model=OPENCODE_DEFAULT_MODEL or None,
    default_max_minutes=int(os.environ.get("OPENCODE_DEFAULT_MAX_MINUTES", "10")),  # :42
    version_style="loose",                                                 # :52-58
)


# ═══════════════════ provider_json (shared helper) ═══════════════════
# The Keys secret for a provider-parameterized harness (kimi today) is a
# JSON provider spec — single {base_url, model?, api_key?, ...}, a multi
# {"providers": {...}, "default": ...} map, or a bare key harness_core wraps
# to spec.bare_key_base_url. This resolves WHICH provider + model id a run
# uses; the spec's own argv/env builders consume the result.


def _resolve_provider_json(creds: dict, model) -> tuple:
    """(provider_spec, model_id) for THIS run — generic over the single / multi
    / bare shapes harness_core._read_creds returns for creds_shape=
    "provider_json". Multi-provider creds route by
    model-slug prefix: 'deepinfra/Qwen/Qwen3-...' → providers['deepinfra'] +
    'Qwen/Qwen3-...'; no matching prefix → the default provider, slug as-is
    (so OpenRouter's own provider-bearing slugs like 'qwen/qwen3-coder' still
    work when openrouter is the default). Single-spec/bare creds resolve to
    themselves."""
    slug = (model or "").strip()
    provs = creds.get("providers")
    if not isinstance(provs, dict):
        return creds, slug or str(creds.get("model") or "")
    head, sep, rest = slug.partition("/")
    if sep and head.strip().lower() in provs and rest.strip():
        p = provs[head.strip().lower()]
        return p, rest.strip()
    p = provs[creds["default"]]
    return p, slug or str(p.get("model") or "")

# ══════════════════════════════════ kimi ════════════════════════════════════
# Kimi Code — Moonshot AI's coding CLI (www.kimi.com/code). PROVIDER-
# PARAMETERIZED on the provider_json contract: the Keys secret
# 'harness-kimi' is EITHER a bare vendor key (auto-wrapped to Moonshot's API by
# harness_core, per spec.bare_key_base_url) OR a JSON provider spec
# {base_url, model?, api_key?, type?} — including the multi-provider
# {"providers": {...}, "default": ...} shape, resolved by
# _resolve_provider_json above. `type` picks the wire the binary speaks: kimi (default) /
# openai / anthropic — all three present in the 0.32.0 binary.
#
# BRAND HOME: the binary keeps sessions/logs/its own stub config under
# $HOME/.kimi-code (KIMI_CODE_HOME can move it; HOME is what we set). The pod
# rootfs is read-only and the creds mount is RO, so HOME is pointed at the
# workspace volume (the one RW mount). The runtime
# writes NOTHING there itself: the provider for a run — endpoint, model, key,
# effort — rides the KIMI_MODEL_* env family that build_env sets, and the
# binary keeps that provider in memory (see _KIMI_PROVIDER_TYPES below).

_kimi_log = logging.getLogger("kimi-sidecar")

KIMI_BIN = Path("/var/lib/cseries/apps/kimi/bin/kimi")
KIMI_CREDS_PATH = Path(os.environ.get(
    "KIMI_CREDS_PATH", "/home/privatae/.kimi-code/provider.json"))
# The endpoint a BARE key means. Same default as the platform's own `kimi`
# registry row (server/vault/providers.py) and as the binary's own
# KIMI_BASE_URL default — the three agree by construction, not by luck.
KIMI_BARE_KEY_BASE_URL = os.environ.get("MOONSHOT_BASE_URL",
                                        "https://api.moonshot.ai/v1")
# Kimi-for-Coding SUBSCRIPTION keys live on their own base, not the Moonshot
# platform API (measured 2026-08-10: a subscription key 401s on
# api.moonshot.ai/.cn and serves /models + /chat/completions on this base).
# harness_core probes which base a bare key actually authenticates on —
# platform keys keep the default above, subscription keys resolve here. Same
# env override + default as the kernel's provider registry
# (server/vault/providers._ALT_BASE_URLS), the other half of this contract.
KIMI_SUBSCRIPTION_BASE_URL = os.environ.get("KIMI_SUBSCRIPTION_BASE_URL",
                                            "https://api.kimi.com/coding/v1")
# Derived from WORKSPACE, never a second hardcoded root: the brand home has to
# land on the one RW volume the pod mounts.
KIMI_HOME_DEFAULT = str(WORKSPACE / ".kimi-home")
# Deployment-level override ONLY; empty means unconfigured and the run errors
# instead of guessing a model (no-pins doctrine — same
# contract as OPENCODE_DEFAULT_MODEL above).
KIMI_DEFAULT_MODEL = os.environ.get("KIMI_DEFAULT_MODEL", "")

# The provider for a run is handed to the binary as the KIMI_MODEL_* env family
# (KIMI_MODEL_NAME is the enable switch): the CLI synthesizes a temporary
# provider + model alias IN MEMORY from those variables and writes nothing back
# to config.toml. Measured 2026-08-18 against BOTH the 0.32.0 and 0.34.0
# binaries with an empty $KIMI_HOME: `kimi -p ...` reaches the API on the env
# provider (401 for a bogus key = the key was sent), and config.toml stays the
# vendor's empty stub. Two vendor facts forced this shape, both verified on the
# binaries and in the vendor's CLI reference:
#   * `--prompt` cannot be combined with `--auto`/`--yolo`/`--plan` — the
#     binary exits at startup with "Cannot combine --prompt with --auto", and
#     prompt mode already runs under auto permission. So the argv is bare -p.
#   * From 0.33.0 the CLI reads credentials ONLY from config.toml ([providers]
#     api_key or the [providers.<n>.env] sub-table); process.env KIMI_API_KEY is
#     no longer a fallback. KIMI_MODEL_* is the one in-memory path left, and it
#     is the only one that keeps the key off the workspace volume.
# No --model either: `-m <alias>` outranks KIMI_MODEL_*, and with no alias in
# config.toml the run cannot resolve against the binary's own catalog — the
# same containment the old per-run alias gave.
#
# provider spec `type` -> KIMI_MODEL_PROVIDER_TYPE. The binary accepts exactly
# these three (kimi / anthropic / openai); anything else falls back to the
# Moonshot wire, since a bare key wraps to Moonshot.
_KIMI_PROVIDER_TYPES = ("kimi", "openai", "anthropic")

# Efforts the binary accepts for a thinking level. Sourced from the vendor's
# own shipped model catalog (support_efforts on the managed kimi-code models),
# not from docs. An effort outside this set is DROPPED with a log line rather
# than passed — a bad value would make the run carry a provider the binary
# rejects, and losing a thinking knob must never lose the run.
_KIMI_EFFORTS = ("low", "high", "max")

# The binary REQUIRES a positive max_context_size on every model entry — its
# schema is `number().int().min(1)` and a missing one makes the alias fail to
# register (`kimi doctor` errors, `provider list` shows models=0; caught live
# against 0.32.0, not inferred). It caps maxInputSize, so it is a context
# budget rather than a capability claim. Default = 262144, the value the vendor
# ships for three of its own four catalog models; a provider spec may override
# per credential and this env var overrides per deployment.
KIMI_MAX_CONTEXT_SIZE = int(os.environ.get("KIMI_MAX_CONTEXT_SIZE", "262144"))


# Resolved per call (like GROK_RUN_FLAG) so tests can point it at a temp dir.
def _kimi_home() -> Path:
    return Path(os.environ.get("KIMI_HOME", KIMI_HOME_DEFAULT))


def _kimi_normalize_model(raw) -> str:
    """The model id for THIS run. Accepts either the NATIVE Moonshot id the
    Orchestrator Coding picker offers (inference.HARNESS_MODEL_VENDORS["kimi"]
    lists native ids, vendor prefix stripped) or a full prefixed slug, so a
    value copied straight off a catalog row still dials. Only Moonshot's own
    prefixes are stripped — a custom endpoint's 'Vendor/Model' id is left
    intact. No model at all is UNCONFIGURED, never a default; the error
    propagates (make_run normalizes OUTSIDE its try)."""
    model = (raw or "").strip() or KIMI_DEFAULT_MODEL
    if not model:
        raise ValueError(
            "kimi has no model configured — pick one in the Orchestrator "
            "Coding section")
    head, sep, rest = model.partition("/")
    if sep and rest.strip() and head.strip().lower() in ("moonshotai", "moonshot"):
        model = rest.strip()
    return model


# The provider resolved for the CURRENT run, plus the model id and accepted
# effort. build_env(spec, creds) has no model argument, so the argv builder
# (which harness_core calls FIRST) stashes what the env builder needs.
# Keys: the provider spec fields
# (base_url / api_key / type / max_context_size) + "_model" + "_effort".
_KIMI_RUN_PROVIDER: dict = {}


def _kimi_resolve_run(spec, model, effort=None) -> None:
    """Resolve the provider for THIS run and stash it for _kimi_env: the user's
    endpoint, the model id, and the thinking effort when one was both requested
    and accepted. Done from the argv builder because the nullary derive hook
    cannot see the per-run model. NOTHING is
    written to disk: the whole provider rides in the KIMI_MODEL_* env of the
    child process (see _KIMI_PROVIDER_TYPES above for why)."""
    _KIMI_RUN_PROVIDER.clear()
    creds = _read_creds(spec)
    if not isinstance(creds, dict):
        return  # /run already 401'd via run_auth_ok before argv is built
    # Generic over the single / multi / bare shapes harness_core returns.
    creds, model_id = _resolve_provider_json(creds, model)
    _KIMI_RUN_PROVIDER.update(creds)
    _KIMI_RUN_PROVIDER["_model"] = model_id
    eff = str(effort or "").strip().lower()
    if eff and eff in _KIMI_EFFORTS:
        _KIMI_RUN_PROVIDER["_effort"] = eff
    elif eff:
        _kimi_log.info("kimi: dropping unsupported effort %r (accepts %s)",
                       eff, ", ".join(_KIMI_EFFORTS))


def _kimi_argv(spec, prompt, project_dir, model, effort=None) -> list:
    # One-shot headless contract: bare -p. Prompt mode runs under the binary's
    # auto permission mode by itself, and it REJECTS --auto/--yolo/--plan at
    # startup ("Cannot combine --prompt with --auto" — measured on 0.32.0 and
    # 0.34.0, and stated in the vendor's flag-conflict rules). No --model: the
    # KIMI_MODEL_* provider _kimi_env passes IS the model for this run, and an
    # explicit -m would outrank it. The workspace is the cwd (run_cwd=True),
    # which is what the binary treats as its root work dir.
    _kimi_resolve_run(spec, model, effort)
    return [str(spec.binary), "-p", prompt]


def _kimi_env(spec, creds) -> dict:
    env = {**os.environ, "HOME": str(_kimi_home())}
    # The host fetch channel (host_mcp): kimi reads $KIMI_CODE_HOME/mcp.json,
    # and with KIMI_CODE_HOME unset that is $HOME/.kimi-code/mcp.json — under
    # the brand home on the workspace volume, off the project tree. The file
    # names the shim only; the credential stays in this process env, which the
    # spawned shim inherits. No-op when the operator injected no channel.
    host_mcp.write_kimi_mcp_json(_kimi_home())
    # Tethers off, by env because the binary has no config key for these
    # (names verified in 0.32.0 and still present in 0.34.0). A pod must not
    # phone home or schedule work.
    env["KIMI_DISABLE_TELEMETRY"] = "1"
    env["KIMI_DISABLE_CRON"] = "1"
    # Only the stash build_argv filled for THIS run: it is the one place the
    # resolved model lives, and KIMI_MODEL_NAME is the family's enable switch.
    # An empty stash (creds vanished between harness_core's read and ours)
    # therefore passes no provider at all and the run fails closed at the
    # binary — never a half-provider, never a key without a model.
    prov = _KIMI_RUN_PROVIDER
    key = prov.get("api_key")
    if key:
        wire = str(prov.get("type") or "").strip().lower()
        if wire not in _KIMI_PROVIDER_TYPES:
            # Unset or unrecognised: a bare key wraps to Moonshot, so its own
            # protocol is the only sound assumption.
            wire = "kimi"
        ctx = prov.get("max_context_size")
        if not isinstance(ctx, int) or ctx < 1:
            ctx = KIMI_MAX_CONTEXT_SIZE
        env["KIMI_MODEL_API_KEY"] = str(key)
        env["KIMI_MODEL_PROVIDER_TYPE"] = wire
        env["KIMI_MODEL_MAX_CONTEXT_SIZE"] = str(ctx)
        if prov.get("base_url"):
            env["KIMI_MODEL_BASE_URL"] = str(prov["base_url"])
        # KIMI_MODEL_NAME is the enable switch for the whole family; without a
        # resolved model the run already failed normalize_model upstream.
        if prov.get("_model"):
            env["KIMI_MODEL_NAME"] = str(prov["_model"])
        if prov.get("_effort"):
            env["KIMI_MODEL_THINKING_EFFORT"] = str(prov["_effort"])
    return env


KIMI = HarnessSpec(
    id="kimi", display="kimi", logger=_kimi_log, log_format=LOG_FMT,
    binary=KIMI_BIN, workspace=WORKSPACE,
    cache_dir=None,
    # Binary BAKED at image build (sidecars/harness-kimi/Dockerfile); no
    # runtime download, so no bootstrap 503s in /run.
    baked=True, cache_valid=None, acquire=None, release_tag=None,
    creds_path=KIMI_CREDS_PATH, creds_shape="provider_json",
    bare_key_base_url=KIMI_BARE_KEY_BASE_URL,
    bare_key_alt_base_urls=(KIMI_SUBSCRIPTION_BASE_URL,),
    auth_error=("kimi not connected — add your Kimi (Moonshot) API key in the "
                "Keys app as 'harness-kimi' (a bare key, or JSON "
                "{base_url, model, api_key})"),
    build_argv=_kimi_argv, build_env=_kimi_env,
    run_cwd=True,
    timeout_error=lambda m: f"kimi exceeded {m}min cap",
    status_tail=(),
    # A usable single spec carries base_url, a multi one carries providers
    # (harness_core only returns entries that have base_url).
    run_auth_ok=lambda spec, creds: isinstance(creds, dict)
        and bool(creds.get("base_url") or creds.get("providers")),
    normalize_model=_kimi_normalize_model,
    report_model=lambda m: m,   # always the normalized id, never "default"
    default_model=KIMI_DEFAULT_MODEL or None,
    default_max_minutes=int(os.environ.get("KIMI_DEFAULT_MAX_MINUTES", "10")),
    version_style="loose",   # `kimi --version` prints a bare semver
)


# ═══════════════════════════════ antigravity ════════════════════════════════
# Antigravity CLI (`agy`) — Google's agentic coding CLI. It is the vendor tool
# for Google AI Pro / Ultra accounts: Gemini CLI stopped serving free / AI Pro /
# AI Ultra sign-ins on 2026-06-18 (google-gemini/gemini-cli discussion #27274)
# and those plans run through `agy` now, which is why this harness is agy and
# not gemini. Closed source; obtained as a pinned, sha512-verified release
# tarball (sidecars/harness-antigravity/Dockerfile).
#
# Everything below was MEASURED against the pinned binary (1.1.17,
# 2026-08-21) — not read from docs:
#
#   * `agy --help`: the non-interactive surface is -p/--print <prompt>,
#     --output-format text|json|stream-json, --input-format, --model,
#     --effort low|medium|high, --dangerously-skip-permissions,
#     --print-timeout (default 5m0s; 0s = no limit since 1.3.0), -c/--continue, --conversation, --sandbox.
#     There is NO `login` subcommand: sign-in is what the binary does on first
#     use (`agy models` unauthenticated: "Please sign in … Launch the CLI
#     without arguments to sign in").
#   * Unauthenticated `agy -p … </dev/null` prints to STDERR
#         Authentication required. Please visit the URL to log in:
#           https://accounts.google.com/o/oauth2/auth?…&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&…
#         Waiting for authentication (timeout 60s)...
#         Or, paste the authorization code here and press Enter:
#     then reads the code from STDIN. The redirect is Google-hosted (it shows
#     the user the code), so no local callback port exists and the flow works
#     from a pod; after 60 s with no code it exits 1 ("authentication timed
#     out") — a fresh POST /login issues a fresh URL; a wrong code is refused
#     at once ("token exchange failed: oauth2: "invalid_grant"", exit 1 within
#     0.2 s — measured in the built image). That is the claude-code stdin-code
#     shape, served by harness_core.make_login_code. ONE CATCH, measured the
#     same way: stdin must NOT be a pipe — a piped stdin makes the binary
#     treat the run as CI ("Error: authentication required. Run 'agy' to log
#     in, then retry.", exit 1, no URL); /dev/null or a tty gets the URL loop,
#     and the URL is flushed to a piped stdout within 0.1 s. make_login
#     therefore hands the login a pseudo-terminal for stdin only.
#   * Token storage: the OS keyring when one is reachable, else a FILE —
#     "Failed to load token from keyring, falling back to file: The name
#     org.freedesktop.secrets was not provided by any .service files" in a
#     clean env with no D-Bus (what a pod is) — and strace shows the one file
#     it probes for it:  $HOME/.gemini/antigravity-cli/antigravity-oauth-token
#   * $HOME/.gemini/antigravity-cli/settings.json is the CLI's settings file.
#     `"modelProvider": "gemini"` + GEMINI_API_KEY runs it against the Gemini
#     API on a key instead of a Google sign-in ("Only setting a GEMINI_API_KEY
#     environment variable on its own has no effect" — vendor docs; the
#     changelog entry for 1.1.13 is baked into the binary); "telemetryEnabled"
#     is a settings key present in the binary.
#   * `--output-format json` result envelope: {conversation_id, status,
#     response, error, duration_seconds, num_turns, usage{…}} (measured:
#     {"status":"ERROR","error":"authentication failed or timed out",…}).
#
# Re-measured on 1.3.0 (2026-10-06, signed out): --help
# keeps every flag above with the same meaning (--effort now also takes
# xhigh|max, not offered here); still no `login` subcommand; the
# /dev/null-stdin URL + 60 s code wait + exit 1 + json envelope, the
# piped-stdin CI error, `agy models`' sign-in refusal and the token file path
# (strace) are unchanged. New: stderr ends with an `error: <reason>` line.
# Per the vendor's own changelog (NOT measured): an agent/model API failure
# exits 3 with an `AGY_ERROR: {…}` stderr line, and a headless run now waits
# for its background tasks up to the --print-timeout deadline, capped at 30
# minutes. Not re-measured on 1.3.0: the pty login path, a wrong-code
# refusal, the Gemini-key lane, and anything that needs a signed-in run.
# The binary also replaces ITSELF on disk when it can (measured: a writable
# 1.1.17 copy became 1.3.0 after `agy models`); harness pods run with a
# read-only rootfs (the operator's _harness_security_context), which is what
# keeps the pin a pin, and the binary stayed intact in the read-only image.
#
# Two lanes, grok's dual shape: the vendor's own sign-in (the Google account
# the user pays their plan on — the primary lane, driven by the host) and a
# Keys-app paste of a Gemini API key (`harness-antigravity`) for the
# key-billed path. The run takes the token when one is present, else the key.
#
# $HOME: the rootfs is read-only and the binary keeps its session file, logs,
# conversation db and settings under $HOME/.gemini, so HOME is pointed at the
# workspace volume (the ONLY writable path) — exactly kimi's KIMI_HOME move.
# The creds the pod is woken with land in /home/privatae/.antigravity
# (the operator's creds_dir); the entity-stored token there is SEEDED into the
# vendor's file before every run and at sidecar start, and the vendor's file
# is what /login, /login/status, /status and the per-run rotation read, so a
# token the binary refreshes in the pod rides back as rotated_creds.

_agy_log = logging.getLogger("antigravity-sidecar")

AGY_BIN = Path("/var/lib/cseries/apps/antigravity/bin/agy")
AGY_RELEASE_VERSION = os.environ.get("AGY_RELEASE_VERSION", "1.3.0")
AGY_CREDS_DIR = Path("/home/privatae/.antigravity")
# The Keys-app paste (harness-antigravity): a bare Gemini API key, or the
# {"api_key": …} envelope the Keys ingest may wrap it in.
AGY_KEY_PATH = Path(os.environ.get("AGY_API_KEY_PATH", str(AGY_CREDS_DIR / "api_key")))
# The vendor's own token file NAME (measured; see the header). Same name on
# the entity side (review._HARNESS_CRED_FILES) so the export needs no rename.
AGY_TOKEN_NAME = "antigravity-oauth-token"
AGY_ENTITY_TOKEN_PATH = AGY_CREDS_DIR / AGY_TOKEN_NAME
# The vendor's HOME. Resolved ONCE at import — HarnessSpec.alt_auth_path is a
# static Path — and every helper below derives its paths from the SPEC
# (alt_auth_path = the vendor's token file; creds_path = the key file), so a
# test relocates the whole layout with one dataclasses.replace.
AGY_HOME = Path(os.environ.get("AGY_HOME", str(WORKSPACE / ".agy-home")))
AGY_VENDOR_TOKEN_PATH = AGY_HOME / ".gemini" / "antigravity-cli" / AGY_TOKEN_NAME
# `--effort low|medium|high` — the binary's own flag help (1.1.17; 1.3.0 also
# lists xhigh|max, not offered here). Mirrored by
# inference.HARNESS_EFFORTS["antigravity"]; the effort-wiring gate pairs them.
AGY_EFFORTS = ("low", "medium", "high")
# `--print-timeout` defaulted to 5m0s through 1.1.17 (0s = none since 1.3.0),
# which would cut a real build; the
# runtime's own max_minutes deadline governs, so the binary's is made moot.
AGY_PRINT_TIMEOUT = os.environ.get("AGY_PRINT_TIMEOUT", "24h")

# Antigravity's catalog is PER-ACCOUNT, like Cursor's and unlike Codex's: `agy
# models` is a backend read that refuses without a session ("Please sign in to
# view available models", measured on the v0.1.2 image with no creds). So there
# is nothing to mirror in-image, and deriving it from the OpenRouter catalog by
# vendor slug is exactly the mistake that put phantom ids in the picker twice
# (see inference.HARNESS_MODEL_VENDORS). It is read in-pod and cached instead.
#
# Output shape, READ OFF agy 1.1.17 in a live entity pod (2026-08-22), not
# guessed: a "Fetching available models..." preamble line, then one model per
# line as "<id>\t<label>". No default marker is printed.
#   gemini-3.7-flash-high\tGemini 3.7 Flash (High)
#   claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)
# NOTE the ids bake reasoning level in (…-high/-medium/-low) while the binary
# ALSO takes --effort; they are independent knobs and the id wins for the model
# it names. Nothing here narrows AGY_EFFORTS — that stays the flag's own set.
_AGY_CATALOG_PREAMBLE = "Fetching available models"
# LOWERCASE deliberately. Every id the binary actually printed is lowercase
# (gemini-3.7-flash-high, claude-sonnet-4-6, gpt-oss-120b-medium), and the
# prose this must reject is conventionally not — a log prefix is `WARN`/`ERROR`,
# a sentence starts capitalised. If the vendor ever ships an uppercase id this
# drops it, and that fails in the SAFE direction: a missing model the user can
# see and report, rather than a fabricated one cached as real for a day.
_AGY_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_AGY_CATALOG_RETRY_S = 300.0
_AGY_CATALOG_CACHE: dict = {}


def _agy_parse_models(stdout: str) -> list:
    """[{id, label?}] from `agy models` output, in the vendor's own order. An
    empty list is a real answer (an account whose plan serves nothing), never
    an error — the caller decides whether to cache it."""
    from .harness_core import _strip_ansi
    models: list = []
    for raw in _strip_ansi(stdout or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(_AGY_CATALOG_PREAMBLE):
            continue
        mid, sep, label = line.partition("\t")
        mid = mid.strip()
        # Tab-separated is the measured shape; a line without one is a status
        # or error sentence, never a model id. Skipping it beats inventing an
        # id out of prose. The charset test is the second half of that: a
        # future TABBED vendor error ("Error:\tPlease sign in…") would otherwise
        # mint `Error:` as a model and, being a non-empty list, sail straight
        # past the empty-catalog ambiguity guard and cache for a whole day.
        # Real ids are alnum with dots/dashes — gemini-3.7-flash-high,
        # claude-sonnet-4-6, gpt-oss-120b-medium (measured, not assumed).
        if not sep or not mid or not _AGY_MODEL_ID_RE.match(mid):
            continue
        row: dict = {"id": mid}
        if label.strip():
            row["label"] = label.strip()
        models.append(row)
    return models


def _agy_catalog_from_binary(spec, binary=None, timeout: float = 45.0) -> list:
    """The ACCOUNT's model list off the pinned binary. Raises on any failure —
    the caller owns the fallback. Needs the account's session: this is a
    backend read, not an offline table."""
    import subprocess
    from .harness_core import _strip_ansi
    binary = binary or AGY_BIN
    if not (_agy_token_present(spec) or _read_creds(spec)):
        raise RuntimeError(
            "antigravity is not connected — no session to list models with")
    _agy_prepare_home(spec, use_api_key=not _agy_token_present(spec))
    proc = subprocess.run(
        [str(binary), "models"], capture_output=True, text=True, timeout=timeout,
        env=_agy_env(spec, _read_creds(spec)))
    if proc.returncode != 0:
        raise RuntimeError(
            f"exit {proc.returncode}: {_strip_ansi(proc.stderr or proc.stdout or '')[-200:]}")
    return _agy_parse_models(proc.stdout)


def _agy_model_catalog(spec=None) -> list:
    """The ACCOUNT's catalog, read once per process (a failure is retried after
    _AGY_CATALOG_RETRY_S). [] when antigravity is not connected or the vendor
    read failed — never a fabricated list. The entity caches these rows when it
    reads GET /status, same as cursor."""
    import time as _time
    cached = _AGY_CATALOG_CACHE.get("models")
    if cached is not None:
        return [dict(m) for m in cached]
    now = _time.monotonic()
    if now - float(_AGY_CATALOG_CACHE.get("failed_at") or 0.0) < _AGY_CATALOG_RETRY_S:
        return []
    try:
        rows = _agy_catalog_from_binary(spec if spec is not None else ANTIGRAVITY)
    except Exception as exc:  # noqa: BLE001
        _AGY_CATALOG_CACHE["failed_at"] = now
        _agy_log.warning("antigravity: `agy models` unavailable (%s)", exc)
        return []
    _AGY_CATALOG_CACHE["models"] = rows
    return [dict(m) for m in rows]


def _agy_vendor_token(spec) -> Path:
    return Path(spec.alt_auth_path)


def _agy_home(spec) -> Path:
    # <home>/.gemini/antigravity-cli/antigravity-oauth-token -> <home>
    return _agy_vendor_token(spec).parent.parent.parent


def _agy_settings(spec) -> Path:
    return _agy_vendor_token(spec).parent / "settings.json"


def _agy_entity_token(spec) -> Path:
    # Beside the key file in the creds dir the pod was woken with.
    return Path(spec.creds_path).parent / AGY_TOKEN_NAME


def _agy_token_present(spec) -> bool:
    """A Google-account grant is available to this run: the vendor's own file
    (seeded or written here) or the entity copy the pod was woken with."""
    return (alt_oauth_present(_agy_vendor_token(spec))
            or alt_oauth_present(_agy_entity_token(spec)))


def _agy_write_settings(spec, use_api_key: bool) -> None:
    """The CLI's settings.json for the lane this process runs on. Read-merge-
    write so anything the binary wrote for itself survives; our two keys are
    telemetryEnabled (always off — a pod phones nobody) and modelProvider,
    set to "gemini" on the key lane and REMOVED otherwise (left in place it
    would keep steering a signed-in run onto a key that may not exist)."""
    path = _agy_settings(spec)
    cur: dict = {}
    try:
        obj = json.loads(path.read_text())
        if isinstance(obj, dict):
            cur = obj
    except (OSError, ValueError):
        cur = {}
    cur["telemetryEnabled"] = False
    if use_api_key:
        cur["modelProvider"] = "gemini"
    else:
        cur.pop("modelProvider", None)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cur, indent=2))
    tmp.replace(path)


def _agy_prepare_home(spec, use_api_key: bool = False) -> None:
    """Lay out the vendor's HOME for a vendor process: the CLI dir, its
    settings for the chosen lane, and — unless the run is on a Gemini API key
    — the entity's stored token seeded into the vendor's file when that file
    is missing or empty (a warm pod's own copy is the fresher one: the binary
    refreshes it in place and the rotation export carries it back). Never
    raises: a failure here surfaces as the vendor's own auth error, which is
    the honest one."""
    vendor = _agy_vendor_token(spec)
    entity = _agy_entity_token(spec)
    try:
        vendor.parent.mkdir(parents=True, exist_ok=True)
        _agy_write_settings(spec, use_api_key)
        if (not use_api_key and alt_oauth_present(entity)
                and not alt_oauth_present(vendor)):
            tmp = vendor.with_name(vendor.name + ".tmp")
            tmp.write_bytes(entity.read_bytes())
            tmp.chmod(0o600)
            tmp.replace(vendor)
    except OSError as e:
        _agy_log.warning("antigravity: could not prepare %s: %s", _agy_home(spec), e)


def _agy_on_start(spec) -> None:
    _agy_prepare_home(spec, use_api_key=False)


def _agy_argv(spec, prompt, project_dir, model, effort=None) -> list:
    # `-p <prompt>` is the one-shot headless mode (measured: `agy -p "say hi"
    # --output-format json` parsed both flags — -p takes the prompt as its
    # value, so flags after it still parse). --dangerously-skip-permissions
    # = auto-approve tool use so the agent can write files and run commands;
    # the OUTER pod is the sandbox (codex's danger-full-access doctrine), so
    # the binary's own --sandbox is NOT passed. No --cwd flag exists: the
    # binary works in the process cwd (run_cwd=True). No --output-format in
    # the buffered argv (text is the default) — the streaming path appends
    # stream-json through STREAM_FLAGS.
    key = _read_creds(spec) or ""
    _agy_prepare_home(spec, use_api_key=bool(key) and not _agy_token_present(spec))
    cmd = [str(spec.binary), "-p", prompt, "--dangerously-skip-permissions",
           "--print-timeout", AGY_PRINT_TIMEOUT]
    if model:
        cmd.extend(["--model", model])
    eff = str(effort or "").strip().lower()
    if eff:
        if eff in AGY_EFFORTS:
            cmd.extend(["--effort", eff])
        else:
            _agy_log.info("antigravity: dropping effort %r — accepts %s",
                          eff, ", ".join(AGY_EFFORTS))
    return cmd


def _agy_env(spec, creds) -> dict:
    # HOME relocated to the workspace volume (see the header). The Gemini API
    # key rides as GEMINI_API_KEY ONLY on the key lane — with a token present
    # the settings carry no modelProvider and the key must not be in the env,
    # or the binary's choice of lane becomes ambiguous. No SSH_* and no D-Bus
    # in a pod, so the binary's own fallback picks file token storage.
    env = {**os.environ, "HOME": str(_agy_home(spec))}
    if creds and not _agy_token_present(spec):
        env["GEMINI_API_KEY"] = str(creds)
    else:
        env.pop("GEMINI_API_KEY", None)
    return env


def _agy_login_argv(spec) -> list:
    # There is no `login` subcommand; an unauthenticated print-mode run IS the
    # sign-in (it prints the URL, waits for the pasted code, then would run its
    # prompt). make_login_code reaps the process the moment the token lands,
    # so the prompt below never really runs — it is deliberately trivial in
    # case it does. A login is a LOGIN: the pod's copy of any earlier token is
    # dropped first so the binary starts a fresh sign-in instead of
    # short-circuiting into its prompt on a grant the host just decided was
    # not good enough (the entity's copy is untouched and re-seeded on the next
    # run if this login never completes). Called by make_login BEFORE it takes
    # the cred fingerprint, so the unlink is part of the "before" state.
    _agy_prepare_home(spec, use_api_key=False)
    try:
        _agy_vendor_token(spec).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _agy_log.warning("antigravity: could not drop the pod token before login: %s", e)
    return [str(spec.binary), "-p", "Reply with the single word ok.",
            "--output-format", "json", "--print-timeout", "2m"]


def _agy_login_env(spec) -> dict:
    # A Google sign-in: never GEMINI_API_KEY here, or the binary would take the
    # key lane and print no URL at all.
    env = {**os.environ, "HOME": str(_agy_home(spec))}
    env.pop("GEMINI_API_KEY", None)
    return env


def _agy_login_export(spec) -> dict:
    """{token-file-name: contents} — the grant as the ENTITY stores it (same
    name, verbatim bytes as text). Serves both login completion (/login,
    /login/status, /login/code) and the per-run rotation snapshot
    (harness_core._export_creds), so a token the binary refreshes during a
    run reaches the entity. {} when there is nothing usable."""
    try:
        text = _agy_vendor_token(spec).read_text()
    except OSError:
        return {}
    return {AGY_TOKEN_NAME: text} if text.strip() else {}


ANTIGRAVITY = HarnessSpec(
    id="antigravity", display="antigravity", logger=_agy_log, log_format=LOG_FMT,
    binary=AGY_BIN, workspace=WORKSPACE,
    cache_dir=None,
    # Binary BAKED at image build (sidecars/harness-antigravity/Dockerfile); no
    # runtime download, so no bootstrap 503s in /run.
    baked=True, cache_valid=None, acquire=None,
    release_tag=lambda: AGY_RELEASE_VERSION,
    # creds_path = the Keys paste (api key), alt_auth_path = the vendor's own
    # token file: grok's raw_or_json_envelope + alt_auth_path shape, so
    # _creds_state reports present/valid/auth_mode the way grok's does.
    creds_path=AGY_KEY_PATH, creds_shape="raw_or_json_envelope",
    creds_fields=("api_key", "key"),
    alt_auth_path=AGY_VENDOR_TOKEN_PATH,
    auth_error=("antigravity not connected — sign in to Google through the host "
                "(it runs Antigravity's own login in your pod and relays the code "
                "Google shows you), or store a Gemini API key in the Keys app as "
                "'harness-antigravity'"),
    build_argv=_agy_argv, build_env=_agy_env,
    run_cwd=True,
    timeout_error=lambda m: f"antigravity exceeded {m}min cap",
    # `models` (2026-08-22) so /status carries what --model can actually take
    # for THIS account — the catalog is per-account, so there is nothing to
    # ship in the image and nothing honest to derive. Same contract as cursor.
    status_tail=("release_tag", "models"),
    models=_agy_model_catalog,
    # The entity copy counts too: on a cold pod the vendor file is seeded from
    # it AFTER this gate (build_argv), so gating on the vendor file alone would
    # 401 the first run of every wake.
    run_auth_ok=lambda spec, key: bool(key) or _agy_token_present(spec),
    default_max_minutes=int(os.environ.get("AGY_DEFAULT_MAX_MINUTES", "10")),
    version_style="strict",           # `agy --version` prints the bare version ("1.3.0") on stdout
    # The vendor's own sign-in, stdin-code shape (see the header + harness_core).
    login_argv=_agy_login_argv, login_env=_agy_login_env,
    login_stdin_code=True,
    login_export=_agy_login_export,
    on_start=_agy_on_start,
)


ALL_SPECS = {"codex": CODEX, "cursor": CURSOR, "grok": GROK,
             "opencode": OPENCODE, "kimi": KIMI,
             "antigravity": ANTIGRAVITY}
