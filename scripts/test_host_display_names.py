#!/usr/bin/env python3
"""A Host with no chosen name is shown as "Host #N", and a pod that is not ready
answers 503 whatever its words say.

The word "entity" never reaches the user; it is "Host". Existing pods still
store placeholder names ("Entity", "Entity 9003", "entity-9003"); there is no
migration, so they are rewritten at display time. Pinned here:

  1. server/host_display.py and the Settings app's isPlaceholderName() agree on
     every case below (one pattern, two languages);
  2. a placeholder shows as "Host #N" (or "Host" with no id), a chosen name
     is shown as it is;
  3. identity_seed still recognises the pre-2026-09-22 "Entity" seed row as the
     placeholder, and the new "Host" seed row too;
  4. the inbox and calendar routes map a not-ready envelope from
     memory_handlers to 503 by its `retryable` flag. They used to match the
     words "entity id"; rewording the message to "host id unknown" would have
     silently turned that 503 into a 502. Every "host id unknown" and "pool not
     ready" envelope in memory_handlers carries the flag.

Container-free: APP_ROOT=<repo> python3 scripts/test_host_display_names.py
"""
import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

FAILS: list[str] = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


# (name, is_placeholder)
# Placeholders are EXACTLY the generated defaults; anything a user could have
# typed on purpose ("Host 7", "Host#12", "Entity #9003") is a name and is shown.
CASES = [
    ("", True), ("   ", True), ("Entity", True), ("entity", True), ("Host", True),
    ("Entity 9003", True), ("entity 9128", True), ("Host #9003", True), ("host #9003", True),
    (" Host #12 ", True), ("entity-9003", True), ("—", True),
    ("Host 7", False), ("host 9003", False), ("Host#12", False), ("Entity #9003", False),
    ("Host-7", False), ("Entity  9003", False),
    ("Nova", False), ("Nova Prime", False), ("Hostess", False),
    ("Entity Framework fan", False), ("Host of the party", False), ("entity-x", False),
]

# ── 1 + 2. Python helper ─────────────────────────────────────────────────
from server.host_display import display_name_or_host, is_placeholder_name  # noqa: E402

for name, want in CASES:
    check(is_placeholder_name(name) == want, f"python: {name!r} placeholder={want}")
check(is_placeholder_name(None), "python: None is a placeholder")
check(display_name_or_host("Entity 9003", 9003) == "Host #9003", "stored 'Entity 9003' shows as 'Host #9003'")
check(display_name_or_host("Entity", 9128) == "Host #9128", "stored seed 'Entity' shows as 'Host #9128'")
check(display_name_or_host("entity-7", "7") == "Host #7", "stored 'entity-7' shows as 'Host #7'")
check(display_name_or_host("", None) == "Host", "no name and no id shows as 'Host'")
check(display_name_or_host("Nova ", 9003) == "Nova", "a chosen name is shown as chosen")
check(display_name_or_host("Host 7", 9003) == "Host 7", "a chosen 'Host 7' is shown as chosen")

# The first line of the identity prompt reads naturally either way.
from server.host_display import host_self_intro  # noqa: E402
check(host_self_intro("Entity 9003", 9003) == "You are the user's Host (Host #9003).",
      "self-intro for a placeholder: 'You are the user's Host (Host #9003).'",
      host_self_intro("Entity 9003", 9003))
check(host_self_intro("", None) == "You are the user's Host.", "self-intro with no name and no id")
check(host_self_intro("Nova", 9003) == "You are Nova, the user's Host.",
      "self-intro for a chosen name: 'You are Nova, the user's Host.'")
_sh = (ROOT / "server/synaptive_handler.py").read_text()
check("host_self_intro(" in _sh and "the user's Host.\"]" not in _sh,
      "synaptive identity prompt opens with host_self_intro()")

# ── 1. the Settings twin ─────────────────────────────────────────────────
src = (ROOT / "system-apps/settings/app.js").read_text()
m = re.search(r"function isPlaceholderName\(name\) \{.*?\n    \}", src, re.S)
check(m is not None, "settings app.js defines isPlaceholderName()")
check("/^entity\\s+\\d+$/i" not in src, "settings app.js no longer carries the entity-only regex")
node = shutil.which("node")
if m and node:
    prog = m.group(0) + "\nconsole.log(JSON.stringify(%s.map(isPlaceholderName)));" % json.dumps(
        [c[0] for c in CASES])
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
    try:
        got = json.loads(out.stdout)
    except ValueError:
        got = None
    check(got == [c[1] for c in CASES], "settings isPlaceholderName() agrees with server/host_display.py",
          f"stdout={out.stdout!r} stderr={out.stderr[:300]!r}")
elif m:
    check(False, "node is needed to run the Settings twin")

# ── 3. identity_seed ─────────────────────────────────────────────────────
from server.bridge import identity_seed as S  # noqa: E402

for seed_name in ("Entity", "Host"):
    row = {"display_name": seed_name, "seed_weights": dict(S.PLACEHOLDER_WEIGHTS),
           "behavioral_params": {}}
    check(S._is_placeholder(row), f"identity_seed: a {seed_name!r} seed row is the placeholder")
row = {"display_name": "Nova", "seed_weights": dict(S.PLACEHOLDER_WEIGHTS), "behavioral_params": {}}
check(not S._is_placeholder(row), "identity_seed: a named row is not the placeholder")
# The first-run identity (server/local_identity.py) seeds the placeholder's own
# neutral weights, so a Host in use that is named "Host" and has lost its
# response_style must still be told apart by what a reseed would overwrite.
from server.local_identity import NEUTRAL_WEIGHTS  # noqa: E402
check(NEUTRAL_WEIGHTS == S.PLACEHOLDER_WEIGHTS,
      "local_identity seeds the placeholder's weights (why the checks below exist)")
fresh = {"display_name": "Host", "seed_weights": dict(S.PLACEHOLDER_WEIGHTS), "behavioral_params": {},
         "current_weights": dict(S.PLACEHOLDER_WEIGHTS), "master_directive": ""}
check(S._is_placeholder(fresh), "identity_seed: the init-db row, all columns read, is the placeholder")
check(S._is_placeholder({**fresh, "current_weights": json.dumps(S.PLACEHOLDER_WEIGHTS)}),
      "identity_seed: ...with its weights as JSON text, as asyncpg returns them")
check(not S._is_placeholder({**fresh, "current_weights": {**S.PLACEHOLDER_WEIGHTS, "warmth": 0.7}}),
      "identity_seed: a 'Host' row whose personality has drifted is not the placeholder")
check(not S._is_placeholder({**fresh, "master_directive": "Look after my garden notes."}),
      "identity_seed: a 'Host' row with a directive is not the placeholder")
init_sql = (ROOT / "init-db.sql").read_text()
check(re.search(r"INSERT INTO entity_registry \(.*?\) VALUES \(\s*'personal',\s*'Host',", init_sql, re.S)
      is not None, "init-db.sql seeds new rows as 'Host'")
check(S.PLACEHOLDER_NAME == "Host", "identity_seed.PLACEHOLDER_NAME mirrors init-db.sql")

# ── 4. not-ready envelopes map to 503 by flag ────────────────────────────
from fastapi import HTTPException  # noqa: E402
from server import brain_service  # noqa: E402
from server.bridge import memory_handlers as mh  # noqa: E402
from server.routes import calendar as cal_routes  # noqa: E402
from server.routes import inbox as inbox_routes  # noqa: E402


def _code(unwrap, env):
    try:
        unwrap(env)
    except HTTPException as e:
        return e.status_code
    return 200


_saved = (brain_service.get_pool, brain_service._entity_id, os.environ.get("ENTITY_ID"))
try:
    brain_service.get_pool = lambda: object()
    brain_service._entity_id = None
    os.environ["ENTITY_ID"] = "0"
    for label, handler, unwrap in (
            ("calendar", mh.handle_calendar_list, cal_routes._unwrap),
            ("inbox", mh.handle_inbox_list, inbox_routes._unwrap)):
        env = asyncio.run(handler({}))
        check(env.get("ok") is False and env.get("retryable") is True,
              f"{label}: a pod with no host id answers a retryable envelope", repr(env))
        check("entity" not in str(env.get("error", "")).lower(),
              f"{label}: the not-ready message does not say 'entity'", repr(env))
        check(_code(unwrap, env) == 503, f"{label}: that envelope is a 503", repr(env))
    brain_service.get_pool = lambda: None
    env = asyncio.run(mh.handle_inbox_list({}))
    check(_code(inbox_routes._unwrap, env) == 503, "inbox: no pool is a 503", repr(env))
finally:
    brain_service.get_pool, brain_service._entity_id = _saved[0], _saved[1]
    if _saved[2] is None:
        os.environ.pop("ENTITY_ID", None)
    else:
        os.environ["ENTITY_ID"] = _saved[2]

for label, unwrap in (("calendar", cal_routes._unwrap), ("inbox", inbox_routes._unwrap)):
    check(_code(unwrap, {"ok": False, "error": "anything at all", "retryable": True}) == 503,
          f"{label}: the flag decides, not the words")
    check(_code(unwrap, {"ok": False, "error": "event_id required"}) == 400,
          f"{label}: a client error is still a 400")
    check(_code(unwrap, {"ok": False, "error": "upstream exploded"}) == 502,
          f"{label}: anything else is still a 502")

tree = ast.parse((ROOT / "server/bridge/memory_handlers.py").read_text())
unflagged = []
for n in ast.walk(tree):
    if isinstance(n, ast.Dict):
        kv = {k.value: v for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant)}
        err = kv.get("error")
        if isinstance(err, ast.Constant) and err.value in ("host id unknown", "pool not ready"):
            flag = kv.get("retryable")
            if not (isinstance(flag, ast.Constant) and flag.value is True):
                unflagged.append(n.lineno)
check(not unflagged, "memory_handlers: every not-ready envelope carries retryable=True",
      f"lines {unflagged}")

if FAILS:
    print(f"\nFAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("\nall checks passed")
