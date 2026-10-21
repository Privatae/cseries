"""Browser agents — a task the user recorded once in the Browser app,
replayed by a real browser with no model in the loop.

The use case: log into a bank and export the ledger every day, with no LLM
doing it. An agent the user built in the browser does the work, and the host
or an app can call it autonomously, with no model involved.

## The agent file

One YAML per agent under <state root>/agents/<name>/agent.yaml, on the
entity's own disk, readable and editable by the user. The steps address a
page the way the sidecar's sessions lane does (sidecars/browser/control/
sessions.py): by role + visible name, resolved from a fresh snapshot at
replay time. No pixel positions, no CSS paths.

    name: bank-ledger-export
    site: online.bank.example        # the session's allow-list, + `allow`
    allow: [login.bank.example]             # extra hosts the recording touched
    steps:
      - go: https://online.bank.example/
      - type:   {field: "User ID", key: "user-id"}      # a SEALED sign-in entry
      - secret: {field: "Password", key: "password"}    # a sealed PASSWORD
      - type:   {field: "Search", literal: "ledger"}    # a value kept in the file
      - click:  {role: button, name: "Sign On"}
      - wait_for_load: true
      - wait_for: "Accounts"
      - pause_if: {text: "Enter the code", field: "Code", submit: true}
      - select: {field: "Format", value: "CSV"}
      - click:  {role: button, name: "Download"}
      - expect_download: {match: "*.csv", save_as: "exports/bank/{date}.csv"}
      - press: Enter
      - switch_page: {index: 1}
      - scroll: 600
      - wait: 1000
    schedule: {recurrence: daily, fire_at: "2026-09-05T06:00:00Z"}

A `type` / `secret` step never carries a value. `key` names a sealed
entry (`form:<site>:<key>`, the same envelope "save these entries" writes)
and the vault broker types it — a value saved as a password can only ever
go into a real password box (server/browser_autofill.fill). `literal` is
the one way a value lives in the file, and only the user puts it there.

## A run

wake the browser → open an isolated session with the agent's allow-list and
the login cookies sealed from the last run → replay the steps → collect
each download the steps expected and write it where the agent said → seal
the cookies back → close the session → write the run record. A failed step
stops the run with the step number, the reason, a masked screenshot and
the page text around it. Nothing improvises a missing step.

`pause_if` is the one place a person can be needed: if the text is on the
page (a one-time code prompt), the run parks, records the question, and
waits for `resume(name, answer)` — from the host's tool, the Browser app,
or the app API — for up to PAUSE_TIMEOUT_S. Nobody answers → the run
FAILS with that reason. It never hangs.

Unattended runs need the site's standing sign-in grant (Keys → Sign-ins).
Without it the vault refuses to type, and the run fails saying so.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
import asyncpg
import yaml

from server import browser_autofill as autofill
from server.host_names import HOST_SUFFIXES
from server.vault import api as vault_api
from server.vault import authz

log = logging.getLogger("privatae.browser_agents")

# The entity PVC — the same root the sidecar's cookies.STATE_ROOT names and
# the app store installs under. Overridable for tests only.
STATE_ROOT = Path(os.environ.get("CSERIES_STATE_ROOT", "/var/lib/cseries"))
AGENTS_SUBDIR = "agents"

# Where a run's login cookies are sealed between runs. cred_type is its own
# so Keys can show it for what it is, and so nothing that resolves API keys
# or form entries ever picks it up by accident.
STATE_CRED_TYPE = "browser_state"
STATE_NAME_PREFIX = "browser-state"

_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
_HOST_RX = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")
_KEY_RX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAVE_AS_RX = re.compile(r"^[A-Za-z0-9._/{}\- ]{1,200}$")

STEP_KINDS = frozenset({
    "go", "click", "type", "secret", "press", "select", "wait_for_load",
    "wait_for", "pause_if", "expect_download", "switch_page", "scroll", "wait",
})
_RECURRENCES = frozenset({"hourly", "daily", "weekly", "monthly"})

LOCATE_TIMEOUT_S = float(os.environ.get("BROWSER_AGENT_LOCATE_TIMEOUT_S", "12"))
LOAD_SETTLE_S = float(os.environ.get("BROWSER_AGENT_LOAD_SETTLE_S", "1.5"))
DOWNLOAD_TIMEOUT_S = float(os.environ.get("BROWSER_AGENT_DOWNLOAD_TIMEOUT_S", "90"))
PAUSE_TIMEOUT_S = float(os.environ.get("BROWSER_AGENT_PAUSE_TIMEOUT_S", "900"))
RUN_TIMEOUT_S = float(os.environ.get("BROWSER_AGENT_RUN_TIMEOUT_S", "1500"))


# The two above are the person's limits (runtime/user_limits, 2026-10-20):
# the constants (with the operator's env) are the defaults, the person's value
# wins, and their "no limit" is None (wait_for with no timeout).
def _pause_timeout_s():
    from mastercode.runtime import user_limits
    return user_limits.person_seconds_or("browser_agent.pause_minutes", PAUSE_TIMEOUT_S)


def _run_timeout_s():
    from mastercode.runtime import user_limits
    return user_limits.person_seconds_or("browser_agent.run_minutes", RUN_TIMEOUT_S)
MAX_RUNS_KEPT = 50
MAX_STEPS = 400

# Hosts an agent may never be pointed at — the sidecar refuses them too
# (sessions._reject_host); refusing here says so at SAVE time, not at 03:00.
_FORBIDDEN_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]",
    "169.254.169.254", "metadata.google.internal", "metadata",
    "kubernetes.default", "kubernetes.default.svc",
    "kubernetes.default.svc.cluster.local",
})
_FORBIDDEN_SUFFIXES = (".localhost", ".local", ".internal", ".svc",
                       ".cluster.local", ".privatae.internal")

# Every zone a Host answers on (server/host_names.py). A Host's desktop and
# each of its apps' origins are names in these zones, and the person's own
# session works there: it is where they approve, change settings, and save a
# skill to clear its untrusted mark. An agent carries whatever its own runs
# signed into, and the Host can start it, so an agent is never pointed at any
# name in these zones, this Host's or another's.
_HOST_ZONES = tuple(s.strip(".").lower() for s in HOST_SUFFIXES)
_HOST_ADDRESS = "is a Host's own address, and an agent never goes there"


def _in_host_zone(host: str) -> bool:
    return any(host == z or host.endswith("." + z) for z in _HOST_ZONES)


def _covers_host_zone(base: str) -> bool:
    """Would `*.<base>` match names in a Host zone? `base` is cleaned the way
    reject_host cleans it, so an entry reject_host accepts (`*. example.com`)
    is judged as the name reject_host judged, not as the raw text."""
    b = (base or "").strip().lower().rstrip(".")
    return any(z == b or z.endswith("." + b) for z in _HOST_ZONES)


class AgentError(Exception):
    """Refused or failed. Carries no secret bytes."""


# ── files ─────────────────────────────────────────────────────────────

def agents_root() -> Path:
    return STATE_ROOT / AGENTS_SUBDIR


def _dir(name: str) -> Path:
    return agents_root() / name


def _validate_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _NAME_RX.match(n):
        raise AgentError("name must be 2-40 chars of lowercase letters, digits "
                         "and hyphens, starting and ending alphanumeric")
    return n


def reject_host(host: str) -> Optional[str]:
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return "empty"
    if _in_host_zone(h):
        return f"{h!r} {_HOST_ADDRESS}"
    if h in _FORBIDDEN_HOSTS:
        return f"{h!r} is loopback or cluster infrastructure"
    if any(h == s.lstrip(".") or h.endswith(s) for s in _FORBIDDEN_SUFFIXES):
        return f"{h!r} is an internal suffix, not a public host"
    if re.fullmatch(r"[0-9.]+", h) or ":" in h:
        return f"{h!r} is an IP literal — name the host"
    if "." not in h:
        return f"{h!r} has no dot — not a public hostname"
    if not _HOST_RX.match(h):
        return f"{h!r} is not a usable host name"
    return None


def _locator(raw: Any, *, what: str) -> dict:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raise AgentError(f"{what} needs a target: {{role, name}}")
    name = str(raw.get("name") or raw.get("field") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    nth = raw.get("nth")
    if not name and not role:
        raise AgentError(f"{what}: a target needs a visible name (or a role)")
    out: dict[str, Any] = {"name": name[:120]}
    if role:
        out["role"] = role[:24]
    if nth is not None:
        try:
            out["nth"] = max(0, int(nth))
        except (TypeError, ValueError, OverflowError):
            raise AgentError(f"{what}: nth must be a number")
    return out


def _norm_step(i: int, raw: Any) -> dict:
    """One canonical step, or AgentError naming the step and the problem."""
    if not isinstance(raw, dict) or len(raw) != 1:
        raise AgentError(f"step {i}: a step is one key, e.g. {{click: {{name: 'Sign On'}}}}")
    kind, body = next(iter(raw.items()))
    kind = str(kind)
    if kind not in STEP_KINDS:
        raise AgentError(f"step {i}: unknown step kind {kind!r}; "
                         f"known: {', '.join(sorted(STEP_KINDS))}")
    w = f"step {i} ({kind})"
    if kind == "go":
        url = str(body or "").strip()
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise AgentError(f"{w}: needs an http(s) URL")
        if "\\" in url or "@" in p.netloc:
            raise AgentError(f"{w}: URL contains a backslash or credentials")
        return {"go": url[:2000]}
    if kind == "click":
        return {"click": _locator(body, what=w)}
    if kind in ("type", "secret"):
        if not isinstance(body, dict):
            raise AgentError(f"{w}: needs {{field, key}} or {{field, literal}}")
        loc = _locator({"name": body.get("field") or body.get("name"),
                        "role": body.get("role"), "nth": body.get("nth")}, what=w)
        out: dict[str, Any] = {"field": loc["name"]}
        if "role" in loc:
            out["role"] = loc["role"]
        if "nth" in loc:
            out["nth"] = loc["nth"]
        # The host the entry was SEALED under — the page the form was
        # submitted on, which need not be the agent's site (a login on
        # login.example.com for a task on app.example.com). The vault checks
        # the live page against it, so the step must carry it.
        host = str(body.get("host") or "").strip().lower()
        if host:
            why = reject_host(host)
            if why:
                raise AgentError(f"{w}: host: {why}")
            out["host"] = host
        key = str(body.get("key") or "").strip().lower()
        literal = body.get("literal")
        if kind == "secret":
            if not key or not _KEY_RX.match(key):
                raise AgentError(f"{w}: a secret step needs the sealed entry's key")
            if literal is not None:
                raise AgentError(f"{w}: a secret is never written in the agent file")
            out["key"] = key
            return {"secret": out}
        if key and literal is not None:
            raise AgentError(f"{w}: give key OR literal, not both")
        if key:
            if not _KEY_RX.match(key):
                raise AgentError(f"{w}: {key!r} is not a usable entry key")
            out["key"] = key
        elif literal is not None:
            out["literal"] = str(literal)[:4096]
        else:
            raise AgentError(f"{w}: needs key (a sealed entry) or literal (a value kept here)")
        if body.get("submit"):
            out["submit"] = True
        return {"type": out}
    if kind == "press":
        key = str(body or "").strip()
        if not key or len(key) > 30:
            raise AgentError(f"{w}: needs a key name, e.g. Enter")
        return {"press": key}
    if kind == "select":
        if not isinstance(body, dict):
            raise AgentError(f"{w}: needs {{field, value}}")
        loc = _locator({"name": body.get("field") or body.get("name"),
                        "role": body.get("role"), "nth": body.get("nth")}, what=w)
        value = str(body.get("value") or "").strip()
        if not value:
            raise AgentError(f"{w}: needs the option's value or label")
        out = {"field": loc["name"], "value": value[:200]}
        if "nth" in loc:
            out["nth"] = loc["nth"]
        return {"select": out}
    if kind == "wait_for_load":
        return {"wait_for_load": True}
    if kind == "wait_for":
        text = str(body or "").strip()
        if not text:
            raise AgentError(f"{w}: needs the text to wait for")
        return {"wait_for": text[:200]}
    if kind == "pause_if":
        if not isinstance(body, dict):
            raise AgentError(f"{w}: needs {{text, field, submit?, ask?}}")
        text = str(body.get("text") or "").strip()
        field = str(body.get("field") or "").strip()
        if not text or not field:
            raise AgentError(f"{w}: needs the text that means 'ask me' and the "
                             f"field the answer goes in")
        out = {"text": text[:200], "field": field[:120],
               "submit": bool(body.get("submit", True))}
        if body.get("ask"):
            out["ask"] = str(body["ask"])[:300]
        return {"pause_if": out}
    if kind == "expect_download":
        if isinstance(body, str):
            body = {"match": body}
        if not isinstance(body, dict):
            raise AgentError(f"{w}: needs {{match, save_as?}}")
        match = str(body.get("match") or "*").strip() or "*"
        out = {"match": match[:120]}
        save_as = body.get("save_as")
        if save_as:
            s = str(save_as).strip().replace("\\", "/")
            if not _SAVE_AS_RX.match(s) or s.startswith("/") or ".." in s.split("/"):
                raise AgentError(f"{w}: save_as must be a relative path under your machine's "
                                 f"state root, e.g. exports/bank/{{date}}.csv")
            out["save_as"] = s
        return {"expect_download": out}
    if kind == "switch_page":
        idx = body.get("index") if isinstance(body, dict) else body
        try:
            return {"switch_page": {"index": max(0, int(idx))}}
        except (TypeError, ValueError, OverflowError):
            raise AgentError(f"{w}: needs the page index")
    if kind == "scroll":
        try:
            return {"scroll": int(body if body is not None else 600)}
        except (TypeError, ValueError, OverflowError):
            raise AgentError(f"{w}: needs a pixel count")
    if kind == "wait":
        try:
            return {"wait": max(0, min(int(body), 30000))}
        except (TypeError, ValueError, OverflowError):
            raise AgentError(f"{w}: needs milliseconds")
    raise AgentError(f"{w}: unhandled")


def normalize(spec: dict) -> dict:
    """Validate a spec the user or the Browser app hands over; return the
    canonical file contents. Every refusal names the step."""
    if not isinstance(spec, dict):
        raise AgentError("an agent is a mapping")
    name = _validate_name(str(spec.get("name") or ""))
    site = str(spec.get("site") or "").strip().lower()
    why = reject_host(site)
    if why:
        raise AgentError(f"site: {why}")
    raw_allow = spec.get("allow") or []
    if not isinstance(raw_allow, (list, tuple)):
        raise AgentError("allow must be a list of host names")
    allow: list[str] = []
    for a in raw_allow:
        h = str(a or "").strip().lower()
        if not h or h == site or h in allow:
            continue
        if h.startswith("*."):
            why = reject_host(h[2:])
            if why is None and _covers_host_zone(h[2:]):
                why = f"{h!r} covers names that are a Host's own address, and an agent never goes there"
        else:
            why = reject_host(h)
        if why:
            raise AgentError(f"allow {h!r}: {why}")
        allow.append(h)
    raw_steps = spec.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise AgentError("an agent needs at least one step")
    if len(raw_steps) > MAX_STEPS:
        raise AgentError(f"too many steps ({len(raw_steps)} > {MAX_STEPS})")
    steps = [_norm_step(i + 1, s) for i, s in enumerate(raw_steps)]
    # Every go must stay on the declared hosts — the session would block it
    # anyway, but a file that cannot run should not save.
    for i, s in enumerate(steps, 1):
        if "go" in s:
            h = (urlparse(s["go"]).hostname or "").lower()
            if not _host_allowed(h, [site] + allow):
                raise AgentError(f"step {i}: {h} is not the agent's site or in allow[] — "
                                 f"add it to allow if the task really goes there")
        for k in ("type", "secret"):
            h = (s.get(k) or {}).get("host") if k in s else None
            if h and not _host_allowed(h, [site] + allow):
                raise AgentError(f"step {i}: entry host {h} is not the agent's site or in "
                                 f"allow[] — the page it signs into must be reachable")
    out: dict[str, Any] = {"name": name, "site": site, "allow": allow, "steps": steps}
    desc = str(spec.get("description") or "").strip()
    if desc:
        out["description"] = desc[:500]
    sched = spec.get("schedule")
    if sched:
        if not isinstance(sched, dict):
            raise AgentError("schedule must be {recurrence, fire_at}")
        rec = str(sched.get("recurrence") or "").strip().lower()
        if rec and rec not in _RECURRENCES and not re.fullmatch(r"every:\d+", rec):
            raise AgentError("schedule.recurrence must be hourly, daily, weekly, monthly "
                             "or every:<seconds>")
        fire_at = str(sched.get("fire_at") or "").strip()
        if not fire_at:
            raise AgentError("schedule.fire_at (ISO time of the first run) is required")
        try:
            datetime.fromisoformat(fire_at.replace("Z", "+00:00"))
        except ValueError:
            raise AgentError("schedule.fire_at must be an ISO timestamp")
        out["schedule"] = {"recurrence": rec or None, "fire_at": fire_at}
        if sched.get("event_id"):
            out["schedule"]["event_id"] = str(sched["event_id"])
    return out


def sign_in_hosts(spec: dict) -> list[str]:
    """Every host a run of `spec` signs in on: the site and its `allow`
    hosts, the only hosts the session may visit and so the ones its restored
    login cookies are for (`_execute`), and each host a step has the vault
    type a sealed entry into (`_step`: the step's host, else the site). A
    wildcard allow entry is named as written. A typed Host turn must hold
    `autofill:<host>` for each (runtime/turn_types.py)."""
    site = str(spec.get("site") or "").strip().lower()
    hosts = {site} if site else set()
    hosts.update(str(h).strip().lower() for h in (spec.get("allow") or []))
    for step in spec.get("steps") or []:
        for kind in ("type", "secret"):
            body = step.get(kind) if isinstance(step, dict) else None
            if isinstance(body, dict) and body.get("key"):
                hosts.add(str(body.get("host") or site).strip().lower())
    hosts.discard("")
    return sorted(hosts)


def _host_allowed(host: str, patterns: list[str]) -> bool:
    h = (host or "").lower()
    for raw in patterns:
        p = raw.lower().strip()
        if p.startswith("*."):
            if h.endswith("." + p[2:]):
                return True
        elif h == p:
            return True
    return False


def load_agent(name: str) -> Optional[dict]:
    try:
        n = _validate_name(name)
    except AgentError:
        return None
    p = _dir(n) / "agent.yaml"
    if not p.is_file():
        return None
    # The file is the user's to edit; a broken edit is reported, not hidden,
    # whatever shape it is in. The caller sees the name and the problem, and
    # the list never fails because one file is broken.
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except Exception as e:
        log.warning("agent %s: unreadable file: %s", n, type(e).__name__)
        return {"name": n, "invalid": f"the file is not readable YAML ({type(e).__name__})",
                "site": None, "steps": []}
    if not isinstance(raw, dict):
        return {"name": n, "site": None, "steps": [],
                "invalid": f"the file is a {type(raw).__name__}, not a mapping of "
                           f"name, site, allow and steps"}
    raw.setdefault("name", n)
    try:
        spec = normalize(raw)
        why = None
    except AgentError as e:
        why = str(e)
    except Exception as e:  # noqa: BLE001 — a value no check above expected
        why = f"the file has a value of the wrong kind ({type(e).__name__})"
    if why is not None:
        site, steps = raw.get("site"), raw.get("steps")
        out = {"name": n, "invalid": why,
               "site": site if isinstance(site, str) else None,
               "steps": steps if isinstance(steps, list) else []}
        # Its calendar row still fires. Keep the schedule so a re-save or a
        # delete cancels that row, as it would for a valid file: a scheduled
        # agent that became invalid (one pointed at a Host's own address,
        # refused since 2026-10-16) must not leave a row failing forever.
        sched = raw.get("schedule")
        if isinstance(sched, dict) and sched.get("event_id"):
            out["schedule"] = {"recurrence": sched.get("recurrence") or None,
                               "event_id": str(sched["event_id"])}
        return out
    runs = list_runs(n, limit=1)
    spec["last_run"] = runs[0] if runs else None
    return spec


def list_agents() -> list[dict]:
    root = agents_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not (d / "agent.yaml").is_file():
            continue
        spec = load_agent(d.name)
        if spec is None:
            continue
        item = {"name": spec["name"], "site": spec.get("site"),
                "steps": len(spec.get("steps") or []),
                "schedule": spec.get("schedule"),
                "last_run": spec.get("last_run"),
                "running": d.name in _active}
        if spec.get("invalid"):
            item["invalid"] = spec["invalid"]
        if spec.get("description"):
            item["description"] = spec["description"]
        out.append(item)
    return out


def _write_yaml(name: str, spec: dict) -> Path:
    d = _dir(name)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "agent.yaml"
    body = ("# A browser agent you recorded in the Browser app. Edit freely.\n"
            "# Steps address the page by what you see (role + name). A `key`\n"
            "# names an entry sealed in Keys → Sign-ins; a value never lives here\n"
            "# unless you wrote it as `literal`.\n")
    body += yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(body)
    os.replace(tmp, p)
    return p


async def save_agent(pool: asyncpg.Pool, *, entity_id: int, spec: dict,
                     actor: str) -> dict:
    """Validate, write the file, and make the calendar match the schedule."""
    canon = normalize(spec)
    name = canon["name"]
    if name in _active:
        raise AgentError(f"{name} is running right now — wait for it to finish")
    prev = load_agent(name)
    prev_event = ((prev or {}).get("schedule") or {}).get("event_id")
    new_sched = canon.get("schedule")
    if prev_event:
        await _cancel_event(pool, entity_id, prev_event)
    # File first, then the calendar row, then the file again with the row's
    # id: a failed write never leaves a scheduled row recorded nowhere.
    _write_yaml(name, canon)
    if new_sched:
        canon["schedule"]["event_id"] = await _schedule_event(
            name, new_sched, entity_id=entity_id)
        _write_yaml(name, canon)
    log.info("agent %s saved by %s (%d steps%s)", name, actor, len(canon["steps"]),
             ", scheduled" if new_sched else "")
    return load_agent(name) or canon


async def delete_agent(pool: asyncpg.Pool, *, entity_id: int, name: str) -> bool:
    n = _validate_name(name)
    if n in _active:
        raise AgentError(f"{n} is running right now — wait for it to finish")
    spec = load_agent(n)
    if spec is None:
        return False
    ev = (spec.get("schedule") or {}).get("event_id")
    if ev:
        await _cancel_event(pool, entity_id, ev)
    import shutil
    shutil.rmtree(_dir(n), ignore_errors=True)
    log.info("agent %s deleted", n)
    return True


async def _schedule_event(name: str, sched: dict, *, entity_id: int) -> Optional[str]:
    """One calendar row in the op lane: the offline runner fires
    browser_agent op='run' with no model (brain/offline_task_runner._run_op)."""
    from server.bridge.memory_handlers import handle_calendar_append
    env = await handle_calendar_append({
        "event_type": "scheduled_task",
        "summary": f"Run browser agent {name}",
        "event_at": sched["fire_at"],
        "status": "scheduled",
        "source": "entity",
        "recurrence": sched.get("recurrence"),
        "payload": {"op": {"tool": "browser_agent",
                           "args": {"op": "run", "name": name, "trigger": "schedule"}},
                    "origin": {"interaction_context": "browser-agent"}},
    })
    if not env.get("ok"):
        raise AgentError(f"could not schedule: {env.get('error') or 'calendar refused'}")
    return str(env.get("event_id") or "") or None


async def _cancel_event(pool: asyncpg.Pool, entity_id: int, event_id: str) -> None:
    try:
        await pool.execute(
            "UPDATE calendar_events SET status='cancelled' "
            "WHERE event_id=$1::uuid AND entity_id=$2 AND status='scheduled'",
            event_id, int(entity_id))
    except Exception as e:
        log.warning("cancel of calendar event %s failed: %s", event_id, type(e).__name__)


# ── runs ──────────────────────────────────────────────────────────────

def _runs_dir(name: str) -> Path:
    return _dir(name) / "runs"


def list_runs(name: str, limit: int = 20) -> list[dict]:
    try:
        n = _validate_name(name)
    except AgentError:
        return []
    d = _runs_dir(n)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"), reverse=True)[:max(1, limit)]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    live = _active.get(n)
    if live is not None:
        out = [live.record()] + [r for r in out if r.get("run_id") != live.run_id]
    return out


def _write_run(name: str, rec: dict) -> None:
    d = _runs_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{rec['run_id']}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=1, default=str))
    os.replace(tmp, p)
    files = sorted(d.glob("*.json"))
    for old in files[:-MAX_RUNS_KEPT]:
        try:
            old.unlink()
            png = old.with_suffix(".png")
            if png.exists():
                png.unlink()
        except OSError:
            pass


class _Run:
    def __init__(self, name: str, trigger: str) -> None:
        self.name = name
        self.trigger = trigger
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.finished_at: Optional[str] = None
        self.status = "running"          # running | waiting | ok | failed
        self.step_index = 0              # 1-based, the step in progress
        self.step_total = 0
        self.log: list[dict] = []
        self.outputs: list[dict] = []
        self.error: Optional[str] = None
        self.question: Optional[str] = None
        self.page_text: Optional[str] = None
        self.screenshot: Optional[str] = None
        self.answer: Optional[str] = None
        self.paused = False              # a pause_if fired during this run
        self.state_dropped = 0           # sealed entries for a Host address, dropped
        self.answer_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.done = asyncio.Event()
        self.limit_hit: Optional[str] = None   # the person's limit that stopped it
        self.watchdog: Optional[asyncio.TimerHandle] = None

    def note(self, msg: str, **kw: Any) -> None:
        self.log.append({"ms": int(time.time() * 1000), "step": self.step_index,
                         "msg": msg, **kw})

    def record(self) -> dict:
        rec = {
            "run_id": self.run_id, "agent": self.name, "trigger": self.trigger,
            "status": self.status, "started_at": self.started_at,
            "finished_at": self.finished_at, "step": self.step_index,
            "steps": self.step_total, "outputs": list(self.outputs),
            "error": self.error, "question": self.question,
            "log": self.log[-60:],
        }
        if self.state_dropped:
            rec["state_dropped"] = self.state_dropped
        if self.page_text:
            rec["page_text"] = self.page_text
        if self.screenshot:
            rec["screenshot"] = self.screenshot
        return rec


_active: dict[str, _Run] = {}


def resume(name: str, answer: str) -> tuple[bool, str]:
    """Hand a paused run the user's answer."""
    try:
        n = _validate_name(name)
    except AgentError as e:
        return False, str(e)
    r = _active.get(n)
    if r is None:
        return False, f"{n} is not running"
    if r.status != "waiting":
        return False, f"{n} is not waiting for an answer (status {r.status})"
    r.answer = str(answer)
    r.answer_event.set()
    return True, f"answer passed to {n}; the run continues"


# ── the sidecar client ────────────────────────────────────────────────

# What the Host reads back from the browser control API (a harness pod)
# as an ANSWER: JSON (act results, the session list, a screenshot as base64
# inside the JSON), capped here. A file the user's agent downloaded is not an
# answer and has no cap: _download streams it to disk.
_BROWSER_JSON_MAX = 16 * 1024 * 1024


async def _call(method: str, path: str, payload: Optional[dict] = None,
                timeout: float = 45.0) -> tuple[int, Any]:
    """One call to the browser control API. trust_env OFF: our own pod /
    the harness Service, never the egress proxy (same as authz)."""
    url = f"{authz.browser_base_url()}{path}"
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False) as s:
            async with s.request(method, url, json=payload,
                                 headers=authz.browser_headers()) as resp:
                # The browser runs on the harness pool and renders pages the
                # Host does not control: its answers are read under a cap on
                # decoded bytes, and JSON under a value budget, off the loop
                # when large (server/harness_input.py, 2026-09-28).
                from server import harness_input as _hin
                what = f"browser {method} {path}"
                try:
                    buf = await _hin.read_body(resp, _BROWSER_JSON_MAX, what)
                except _hin.HarnessInputRefused as e:
                    raise AgentError(str(e)) from None
                try:
                    body = await _hin.parse_json(buf, what)
                except _hin.HarnessInputRefused:
                    body = {"detail": bytes(buf[:500]).decode("utf-8", "replace")}
                return resp.status, body
    except asyncio.TimeoutError:
        raise AgentError(f"the browser did not answer {method} {path} within {timeout:.0f}s")
    except aiohttp.ClientError as e:
        raise AgentError(f"could not reach the browser: {type(e).__name__}")


# A download is the user's own file and has no size cap, but the Host's disk
# is shared with everything else it keeps, so a download may not take the
# volume below harness_input.disk_floor (10% of it, at least 256 MiB). And a
# transfer that keeps trickling bytes is still finite: DOWNLOAD_FETCH_TOTAL_S
# bounds the whole fetch, beside the idle window.
DOWNLOAD_FETCH_TOTAL_S = float(os.environ.get("BROWSER_AGENT_DOWNLOAD_FETCH_TOTAL_S", "1800"))


async def _download(path: str, dest: Path, *, idle_timeout: float = 120.0,
                    total_timeout: float | None = None) -> int:
    """GET a file the user's agent downloaded, from the browser control API
    straight to `dest`, and return its size. It is the USER's file, so there
    is no size cap (the browser sidecar puts none on the download either):
    the body is streamed to `<dest>.part` a chunk at a time
    (harness_input.spill_to_file, cap=None), so the Host holds one chunk, not
    the file, and renamed into place only when complete. Refused, with a
    sentence, when it would leave the disk under its floor. Two time limits:
    an IDLE window (no bytes for `idle_timeout` s), so a stalled transfer
    ends, and a TOTAL one (DOWNLOAD_FETCH_TOTAL_S), so a trickle does too.
    A failure to write it (a full disk, a permission) is an AgentError that
    says so, never a raw OSError."""
    from server import harness_input as _hin
    total = DOWNLOAD_FETCH_TOTAL_S if total_timeout is None else total_timeout
    url = f"{authz.browser_base_url()}{path}"
    part = dest.with_name(dest.name + ".part")
    started = time.monotonic()
    try:
        floor = _hin.disk_floor(dest.parent)
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=total, sock_connect=30,
                                              sock_read=idle_timeout),
                trust_env=False) as s:
            async with s.get(url, headers=authz.browser_headers()) as resp:
                if resp.status >= 400:
                    tail = (await _hin.error_tail(resp, 500)).strip()
                    raise AgentError(f"download could not be fetched (HTTP {resp.status})"
                                     + (f": {tail[:200]}" if tail else ""))
                n = await _hin.spill_to_file(resp, part, None, f"the download {dest.name}",
                                             keep_free=floor)
        os.replace(part, dest)
        return n
    except _hin.HarnessInputRefused as e:
        raise AgentError(str(e)) from None
    except asyncio.TimeoutError:
        if time.monotonic() - started >= total - 1:
            raise AgentError(f"the download {dest.name} was still arriving after "
                             f"{total / 60:.0f} minutes, the most a download may take") from None
        raise AgentError(f"the browser sent nothing for {idle_timeout:.0f}s while "
                         f"downloading {dest.name}") from None
    except aiohttp.ClientError as e:
        raise AgentError(f"could not reach the browser: {type(e).__name__}") from None
    except OSError as e:
        raise AgentError(f"could not save the download {dest.name}: "
                         f"{e.strerror or type(e).__name__}") from None
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


def _detail(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or body)[:400]
    return str(body)[:400]


async def _act(session: str, timeout: float = 45.0, **body: Any) -> dict:
    st, out = await _call("POST", f"/v1/sessions/{session}/act", body, timeout=timeout)
    if st >= 400:
        raise AgentError(f"{body.get('action')}: {_detail(out)}")
    return out if isinstance(out, dict) else {}


_BROWSER = "browser"


async def _wake() -> bool:
    """Wake the on-demand browser harness and JOIN THE HOLDERS.

    The return value is kept only so the run can say whether it found the
    browser cold; it no longer decides who releases (_hold/_release do).
    It was never a sound ownership test anyway: `already` reads a CACHED
    upstream that the sweep may have scaled to zero underneath, so a run could
    be told it did not wake a browser it had in fact just woken cold.

    Loopback (the legacy in-pod sidecar) needs no wake; a failed wake is
    reported by the first call, which says the browser is unreachable.

    A wake this machine or the plan REFUSED (a short disk, no room, too many
    containers: harness_backend.wake_refused) raises AgentError with the
    refusal's words, so the run ends there with the reason the owner can act
    on. Calling the browser anyway would fail as "unreachable" and wake it
    cold a second time, to the same refusal.
    """
    try:
        from server.applications import harness_backend as hb
    except Exception as e:  # noqa: BLE001
        log.info("browser wake skipped: %s", type(e).__name__)
        return False
    already = bool(hb.upstream_host(_BROWSER))
    try:
        host = await hb.ensure_awake(_BROWSER, _BROWSER)
    except Exception as e:  # noqa: BLE001
        log.info("browser wake failed: %s", type(e).__name__)
        return not already
    if host is None:
        # Read only after this wake returned None: it recorded its own
        # failure, so the refusal is this wake's, never an earlier one's.
        words = hb.wake_refused_words(_BROWSER)
        if words:
            raise AgentError(f"the browser was not started{words}")
    return not already


def _forget_upstream() -> None:
    """The cached Service may be one the idle sweep already scaled to zero —
    ensure_awake trusts it with no liveness check. Drop it so the next wake
    goes cold (the shape app_verify._browser_verify uses)."""
    try:
        from server.applications import harness_backend as hb
        hb.forget_upstream(_BROWSER)
    except Exception:
        pass


async def _hold() -> None:
    """Join the holders, exactly once per run.

    Deliberately NOT folded into _wake: the unreachable path wakes a second
    time, and a hold inside the wake would count twice against one release and
    leave the browser held forever — the precise failure this change exists to
    remove.
    """
    try:
        from server.applications import harness_backend as hb
        hb.hold_run(_BROWSER, _BROWSER)
    except Exception as e:  # noqa: BLE001
        log.info("browser hold skipped: %s", type(e).__name__)


async def _release() -> None:
    """Leave the holders; the LAST one out puts the browser back to sleep.

    This used to release only when THIS run performed the wake. A run that
    found the browser already awake left it running ("browser left as found"),
    while still refreshing the operator's sweep boundary on its way past — so
    once the original owner vanished, nothing would ever release it and the
    sweep could not fire either. Counting holders makes the last one out
    release it regardless of who woke it.

    release_now still refuses while the user's own Browser viewport holds the
    harness (ws > 0) or another run does (runs > 0), so a run never tears the
    browser down under anyone.
    """
    try:
        from server.applications import harness_backend as hb
        await hb.release_run(_BROWSER, _BROWSER)
    except Exception as e:  # noqa: BLE001
        log.info("browser release failed: %s", type(e).__name__)


def _unreachable(e: AgentError) -> bool:
    return str(e).startswith("could not reach the browser")


async def _notify(subject: str, body: str, *, agent: str, run_id: str,
                  kind: str) -> None:
    """One inbox row to the user. Best-effort: a run must never fail because
    a notice could not be written.

    Why the runner writes it and not the scheduler: the op lane reports a
    scheduled op only when it FAILS (offline_task_runner._report_and_finalize
    with report_on_success=False), and a run that parks for a code returns
    'waiting' — alive, not failed — so without this nobody was told, and the
    timeout that followed 15 minutes later was not reported either because
    the op had already returned."""
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        await handle_inbox_append({
            "subject": subject, "body": body, "body_format": "text/markdown",
            "message_kind": "system_notification",
            "payload": {"browser_agent": agent, "run_id": run_id, "kind": kind},
        })
    except Exception as e:  # noqa: BLE001
        log.info("agent %s: inbox notice not written: %s", agent, type(e).__name__)


# ── locating an element ───────────────────────────────────────────────

_FIELD_ROLES = ("textbox", "combobox", "searchbox", "spinbutton")


def match_element(elements: list[dict], loc: dict, *,
                  prefer_roles: tuple[str, ...] = ()) -> Optional[dict]:
    """The element a step means, from a snapshot. Exact visible name first,
    then a name that starts with it, then one that contains it; a declared
    role narrows every tier; `nth` picks among equals. Pure, so it is
    testable without a browser."""
    want = (loc.get("name") or "").strip().lower()
    role = (loc.get("role") or "").strip().lower()
    nth = int(loc.get("nth") or 0)

    def ok_role(el: dict) -> bool:
        r = str(el.get("role") or "").lower()
        if role:
            if role == "textbox" and r in _FIELD_ROLES:
                return True
            return r == role
        return (r in prefer_roles) if prefer_roles else True

    pool = [el for el in elements if ok_role(el) and not el.get("disabled")]
    if not pool and prefer_roles and not role:
        pool = [el for el in elements if not el.get("disabled")]
    if not want:
        return pool[nth] if nth < len(pool) else None
    tiers = (
        [el for el in pool if str(el.get("name") or "").strip().lower() == want],
        [el for el in pool if str(el.get("name") or "").strip().lower().startswith(want)],
        [el for el in pool if want in str(el.get("name") or "").strip().lower()],
    )
    for tier in tiers:
        if tier:
            return tier[nth] if nth < len(tier) else None
    return None


async def _locate(run: _Run, session: str, loc: dict,
                  prefer_roles: tuple[str, ...] = ()) -> str:
    deadline = time.monotonic() + LOCATE_TIMEOUT_S
    last_count = 0
    while True:
        snap = await _act(session, action="snapshot")
        els = snap.get("elements") or []
        last_count = len(els)
        hit = match_element(els, loc, prefer_roles=prefer_roles)
        if hit is not None:
            return str(hit["ref"])
        if time.monotonic() >= deadline:
            what = loc.get("name") or loc.get("role") or "?"
            raise AgentError(
                f"could not find {what!r}"
                f"{' (' + loc['role'] + ')' if loc.get('role') else ''} on "
                f"{snap.get('url') or 'the page'} — {last_count} elements visible")
        await asyncio.sleep(0.7)


# ── the run ───────────────────────────────────────────────────────────

def _state_name(agent: str) -> str:
    return f"{STATE_NAME_PREFIX}:{agent}"


def _session_name(agent: str) -> str:
    """One sidecar session per agent, never shared: a name too long for the
    sidecar's 40-char limit is shortened with a hash so two long names that
    agree on their first characters do not land on (and delete) each
    other's session."""
    s = f"agent-{agent}"
    if len(s) <= 40:
        return s
    import hashlib
    h = hashlib.sha1(agent.encode("utf-8")).hexdigest()[:6]
    return f"{s[:33].rstrip('-')}-{h}"


def _fill_placeholders(s: str, agent: str) -> str:
    now = datetime.now(timezone.utc)
    return (s.replace("{date}", now.strftime("%Y-%m-%d"))
             .replace("{time}", now.strftime("%H%M%S"))
             .replace("{name}", agent))


def _output_path(agent: str, save_as: Optional[str], filename: str) -> Path:
    root = STATE_ROOT.resolve()
    if save_as:
        rel = _fill_placeholders(save_as, agent)
        if rel.endswith("/"):
            rel = rel + filename
    else:
        # Outside the agent's own directory: deleting an agent must not take
        # the statements it exported with it.
        rel = f"agent-outputs/{agent}/{filename}"
    dest = (root / rel).resolve()
    if root not in dest.parents:
        raise AgentError(f"save_as {save_as!r} leaves your machine's state root")
    return dest


async def _type_sealed(pool: asyncpg.Pool, *, entity_id: int, site: str,
                       session: str, ref: str, key: str, actor: str) -> None:
    try:
        res = await autofill.fill(pool, entity_id=entity_id, host=site, session=session,
                                  mapping=[{"ref": ref, "key": key}], actor=actor)
    except autofill.AutofillError as e:
        # The expected refusal: no standing grant for the site (Keys →
        # Sign-ins), or nothing sealed under that key. Its message carries no
        # secret bytes, and it is the one the user needs to read.
        raise AgentError(str(e))
    if not res.get("filled"):
        why = (res.get("skipped") or [{}])[0].get("reason") or "refused"
        raise AgentError(f"the vault would not type {key!r} for {site}: {why}")


async def _step(run: _Run, spec: dict, step: dict, *, pool: asyncpg.Pool,
                entity_id: int, session: str, actor: str) -> None:
    kind, body = next(iter(step.items()))
    agent = spec["name"]
    site = spec["site"]
    if kind == "go":
        await _act(session, action="navigate", url=body, timeout=60)
        await asyncio.sleep(LOAD_SETTLE_S)
    elif kind == "click":
        ref = await _locate(run, session, body)
        await _act(session, action="click", ref=ref)
    elif kind == "type":
        loc = {"name": body["field"], "role": body.get("role"), "nth": body.get("nth")}
        ref = await _locate(run, session, loc, prefer_roles=_FIELD_ROLES)
        if "key" in body:
            await _type_sealed(pool, entity_id=entity_id, site=body.get("host") or site,
                               session=session, ref=ref, key=body["key"], actor=actor)
            if body.get("submit"):
                await _act(session, action="press", key="Enter")
        else:
            await _act(session, action="type", ref=ref, text=body["literal"],
                       submit=bool(body.get("submit")))
    elif kind == "secret":
        loc = {"name": body["field"], "role": body.get("role"), "nth": body.get("nth")}
        ref = await _locate(run, session, loc, prefer_roles=_FIELD_ROLES)
        await _type_sealed(pool, entity_id=entity_id, site=body.get("host") or site,
                           session=session, ref=ref, key=body["key"], actor=actor)
    elif kind == "press":
        await _act(session, action="press", key=body)
        await asyncio.sleep(LOAD_SETTLE_S)
    elif kind == "select":
        loc = {"name": body["field"], "nth": body.get("nth"), "role": "combobox"}
        ref = await _locate(run, session, loc)
        await _act(session, action="select", ref=ref, values=[body["value"]])
    elif kind == "wait_for_load":
        await asyncio.sleep(LOAD_SETTLE_S)
        await _act(session, action="wait", ms=500)
    elif kind == "wait_for":
        await _act(session, action="wait_for_text", text=body, timeout=60)
    elif kind == "pause_if":
        txt = await _act(session, action="text")
        page = str(txt.get("text") or "").lower()
        if body["text"].lower() not in page:
            run.note(f"'{body['text']}' not shown; continuing")
            return
        question = body.get("ask") or (
            f"{site} is asking for something only you have "
            f"(the page says \"{body['text']}\"). What should go in \"{body['field']}\"?")
        run.status = "waiting"
        run.question = question
        run.answer_event.clear()
        run.note("waiting for the user", question=question)
        run.paused = True
        if run.trigger != "host":
            # Nobody is watching a scheduled or app-triggered run. Say so
            # where the user will see it, with what to do.
            await _notify(
                f"Agent {agent} is waiting for you",
                f"{question}\n\nAnswer in the Browser app → Agents, or tell your host "
                f"the code. " + (f"The run waits {int(_pause_timeout_s() // 60)} minutes."
                                 if _pause_timeout_s() else "The run waits until you answer."),
                agent=agent, run_id=run.run_id, kind="waiting")
        _wait = _pause_timeout_s()
        try:
            await asyncio.wait_for(run.answer_event.wait(), timeout=_wait)
        except asyncio.TimeoutError:
            from mastercode.runtime import user_limits
            user_limits.note_hit(entity_id, "browser_agent.pause_minutes",
                                 f"browser agent {agent} waiting for an answer")
            raise AgentError(f"waited {int(_wait // 60)} minutes for an answer to "
                             f"\"{question}\" and nobody answered. "
                             + user_limits.hit_line("browser_agent.pause_minutes"))
        run.status = "running"
        run.question = None
        answer = run.answer or ""
        run.answer = None
        ref = await _locate(run, session, {"name": body["field"]}, prefer_roles=_FIELD_ROLES)
        await _act(session, action="type", ref=ref, text=answer,
                   submit=bool(body.get("submit", True)))
        await asyncio.sleep(LOAD_SETTLE_S)
    elif kind == "expect_download":
        deadline = time.monotonic() + DOWNLOAD_TIMEOUT_S
        seen = {o.get("source") for o in run.outputs}
        while True:
            dl = await _act(session, action="downloads")
            for d in dl.get("downloads") or []:
                fn = str(d.get("filename") or "")
                if not d.get("ok") or fn in seen or not fnmatch.fnmatch(fn, body["match"]):
                    continue
                dest = _output_path(agent, body.get("save_as"), fn)
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    n = await _download(f"/v1/sessions/{session}/downloads/{fn}", dest)
                except AgentError as e:
                    raise AgentError(f"download {fn!r}: {e}") from None
                run.outputs.append({"source": fn, "path": str(dest), "bytes": n})
                run.note(f"saved {fn} → {dest} ({n} bytes)")
                return
            if time.monotonic() >= deadline:
                raise AgentError(f"no download matching {body['match']!r} arrived within "
                                 f"{int(DOWNLOAD_TIMEOUT_S)}s")
            await asyncio.sleep(1.0)
    elif kind == "switch_page":
        await _act(session, action="switch_page", index=body["index"])
    elif kind == "scroll":
        await _act(session, action="scroll", dy=body)
    elif kind == "wait":
        await _act(session, action="wait", ms=body)
    else:
        raise AgentError(f"unhandled step kind {kind!r}")


async def _capture_failure(run: _Run, session: str) -> None:
    try:
        txt = await _act(session, action="text", timeout=15)
        run.page_text = str(txt.get("text") or "")[:1500]
    except Exception:
        pass
    try:
        shot = await _act(session, action="screenshot", timeout=20)
        b64 = shot.get("png_base64")
        if b64:
            d = _runs_dir(run.name)
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{run.run_id}.png"
            p.write_bytes(base64.b64decode(b64))
            run.screenshot = str(p)
    except Exception:
        pass


async def _seal_state(pool: asyncpg.Pool, *, entity_id: int, agent: str,
                      session: str, actor: str) -> int:
    out = await _act(session, action="export_state")
    state = out.get("storage_state") or {}
    n = len(state.get("cookies") or [])
    if n:
        await vault_api.store_secret(
            pool, entity_id=entity_id, name=_state_name(agent),
            cred_type=STATE_CRED_TYPE,
            plaintext=json.dumps(state).encode("utf-8"), actor=actor)
    return n


async def _open_state(pool: asyncpg.Pool, *, entity_id: int, agent: str,
                      actor: str) -> Optional[dict]:
    try:
        raw = await vault_api.open_secret(pool, entity_id=entity_id,
                                          name=_state_name(agent), actor=actor)
    except Exception as e:
        log.info("agent %s: no sealed state (%s)", agent, type(e).__name__)
        return None
    if not raw:
        return None
    try:
        st = json.loads(raw.decode("utf-8"))
        return st if isinstance(st, dict) else None
    except Exception:
        return None


def _host_of_url(url: Any) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _cookie_reaches_host_zone(c: dict) -> bool:
    """Would the browser send this sealed cookie to a name in a Host zone?
    A domain cookie (`.example.com`, the leading dot) goes to its domain and
    every name under it, so one for the zone's parent reaches every Host; a
    host-only cookie (no dot, or one set by `url`) goes to its one host."""
    dom = str(c.get("domain") or "").strip().lower()
    if dom.startswith("."):
        base = dom.lstrip(".").rstrip(".")
        return bool(base) and (_in_host_zone(base) or _covers_host_zone(base))
    host = dom.rstrip(".") if dom else _host_of_url(c.get("url"))
    return bool(host) and _in_host_zone(host)


def _without_host_zone(state: dict) -> tuple[dict, int]:
    """The sealed browser state minus what a Host's own address would see:
    every cookie the browser would send there, and every stored origin
    (localStorage, IndexedDB) on one. Returns it and how many were dropped.

    Sealed state comes only from this agent's own earlier runs, but those
    runs predate the refusal of Host addresses (2026-10-16), and a cookie
    for a parent domain reaches a Host even from a site that is not one. An
    agent never carries a session into a Host, so the state loses them."""
    out = dict(state)
    dropped = 0
    cookies = state.get("cookies")
    if isinstance(cookies, list):
        kept = [c for c in cookies
                if not (isinstance(c, dict) and _cookie_reaches_host_zone(c))]
        dropped += len(cookies) - len(kept)
        out["cookies"] = kept
    origins = state.get("origins")
    if isinstance(origins, list):
        kept_o = [o for o in origins
                  if not (isinstance(o, dict) and _in_host_zone(_host_of_url(o.get("origin"))))]
        dropped += len(origins) - len(kept_o)
        out["origins"] = kept_o
    return out, dropped


async def _execute(run: _Run, spec: dict, *, pool: asyncpg.Pool, entity_id: int,
                   actor: str) -> None:
    agent = spec["name"]
    session = _session_name(agent)
    run.step_total = len(spec["steps"])
    opened = False
    woke = False
    await _hold()          # released in the finally, whatever happens between
    try:
        woke = await _wake()
        state = await _open_state(pool, entity_id=entity_id, agent=agent, actor=actor)
        if state:
            state, run.state_dropped = _without_host_zone(state)
            if run.state_dropped:
                run.note(f"{run.state_dropped} sealed cookie(s) or stored site entries for a "
                         f"Host's own address dropped; an agent never carries them there")
        run.note("login cookies restored from the vault" if state else "no saved login; starting signed out")
        # A leftover session from a crashed run would refuse the create.
        try:
            await _call("DELETE", f"/v1/sessions/{session}", timeout=30)
        except AgentError as e:
            if not _unreachable(e):
                raise
            # The cached upstream may be a Service the idle sweep already
            # scaled to zero. Forget it and wake cold, once; this run then
            # owns the wake and releases it.
            run.note("browser unreachable at the cached address; waking it cold")
            _forget_upstream()
            await _wake()
            await _call("DELETE", f"/v1/sessions/{session}", timeout=30)
        body: dict[str, Any] = {"name": session, "allow": [spec["site"]] + list(spec.get("allow") or []),
                                "persist": True}
        if state:
            body["storage_state"] = state
        st, out = await _call("POST", "/v1/sessions", body, timeout=60)
        if st >= 400:
            raise AgentError(f"could not open a browser session: {_detail(out)}")
        opened = True
        for i, step in enumerate(spec["steps"], 1):
            run.step_index = i
            kind = next(iter(step))
            run.note(f"{kind}")
            await _step(run, spec, step, pool=pool, entity_id=entity_id,
                        session=session, actor=actor)
        try:
            n = await _seal_state(pool, entity_id=entity_id, agent=agent,
                                  session=session, actor=actor)
            run.note(f"login cookies sealed ({n})")
        except Exception as e:
            run.note(f"could not seal login cookies: {type(e).__name__}")
        run.status = "ok"
    except AgentError as e:
        run.status = "failed"
        run.error = f"step {run.step_index} of {run.step_total}: {e}" if run.step_index else str(e)
        if opened:
            await _capture_failure(run, session)
    except asyncio.CancelledError:
        run.status = "failed"
        run.error = run.limit_hit or "cancelled"
        if run.limit_hit is None:
            raise
        # Our own watchdog's cancel, taken: clear it so the cleanup below
        # (awaits in `finally`) is not cancelled again on its first await.
        _t = asyncio.current_task()
        if _t is not None and hasattr(_t, "uncancel"):
            _t.uncancel()
    except Exception as e:  # noqa: BLE001
        run.status = "failed"
        run.error = f"step {run.step_index}: {type(e).__name__}: {e}"[:400]
        if opened:
            await _capture_failure(run, session)
    finally:
        if run.watchdog is not None:
            run.watchdog.cancel()
        run.finished_at = datetime.now(timezone.utc).isoformat()
        if opened:
            try:
                await _call("DELETE", f"/v1/sessions/{session}", timeout=30)
            except Exception:
                pass
        # One-shot job: leave the holders. The last holder out releases; a
        # viewport or another run still holding keeps it up.
        await _release()
        # Deliberately not "released": with holders, leaving does not imply
        # this run slept the pod — a viewport or another run may still hold it.
        run.note("browser handed back")
        if run.limit_hit is not None:
            # Stopped by the person's limit: said to the person whoever
            # started it (the Host's own call returned long before), and
            # noted for Settings > Limits. The run record below is kept.
            from mastercode.runtime import user_limits
            await user_limits.record_hit(pool, entity_id, "browser_agent.run_minutes",
                                         f"browser agent {agent}")
            await _notify(
                f"Agent {agent} was stopped by your time limit",
                f"{run.limit_hit}\n\nWhat it did is in the Browser app → Agents.",
                agent=agent, run_id=run.run_id, kind="failed")
        elif run.status == "failed" and run.paused and run.trigger != "host":
            # The op lane already returned 'waiting' as ok for this run, so
            # this failure would otherwise reach nobody.
            await _notify(
                f"Agent {agent} failed after waiting for you",
                f"{run.error}\n\nRun it again from the Browser app → Agents, or ask your host.",
                agent=agent, run_id=run.run_id, kind="failed")
        try:
            _write_run(agent, run.record())
        except Exception as e:
            log.warning("agent %s: run record not written: %s", agent, type(e).__name__)
        _active.pop(agent, None)
        run.done.set()
        log.info("agent %s run %s: %s%s", agent, run.run_id, run.status,
                 f" — {run.error}" if run.error else "")


async def run(pool: asyncpg.Pool, *, entity_id: int, name: str, actor: str,
              trigger: str = "host", wait_s: float = 240.0) -> dict:
    """Start a run and wait up to `wait_s` for it to finish. Returns the run
    record either way: status ok / failed when done, `running` or
    `waiting` (with `question`) when it is still going — the caller reads
    the rest later with list_runs, or answers with resume()."""
    n = _validate_name(name)
    spec = load_agent(n)
    if spec is None:
        raise AgentError(f"no agent named {n!r}")
    if spec.get("invalid"):
        raise AgentError(f"agent {n} cannot run: {spec['invalid']}")
    if n in _active:
        return _active[n].record()
    r = _Run(n, trigger)
    _active[n] = r
    r.task = asyncio.create_task(
        _execute(r, spec, pool=pool, entity_id=entity_id, actor=actor))
    _limit = _run_timeout_s()
    if _limit:
        def _stop_at_limit(run=r) -> None:
            from mastercode.runtime import user_limits
            run.limit_hit = (f"The run took {int(_limit // 60)} minutes and was stopped. "
                             + user_limits.hit_line("browser_agent.run_minutes"))
            if run.task is not None and not run.task.done():
                run.task.cancel()
        r.watchdog = asyncio.get_running_loop().call_later(_limit, _stop_at_limit)
    try:
        await asyncio.wait_for(r.done.wait(), timeout=max(0.0, wait_s))
    except asyncio.TimeoutError:
        pass
    return r.record()


def active_runs() -> list[dict]:
    return [r.record() for r in _active.values()]
