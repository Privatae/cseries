"""The machine a C-Series Cloud Host runs on: its vault, its access log and
its restart.

On a cloud box the machine keeps the Host's data in a vault that only the
owner's keys open (vm/cloud, cseries_vault), and writes what the Host may
know of it into CSERIES_MACHINE_DIR, a directory the Host can read and cannot
write (vm/cloud cseries_machine.publish):

  machine-state.json   the vault's state, whether its keys are set up, the
                       unlock page's address, the access log's head and
                       whether it still matches, whether a restart is needed
  access-log.jsonl     every action Privatae took on this machine, and every
                       provider operation it did not start, as the machine
                       holds them
  unlock-record.jsonl  the newest unlock attempts

Without CSERIES_MACHINE_DIR (a Host on a person's own computer) there is no
such machine and every answer here is empty. Until the vault's keys are set
up, /setup refuses to create the owner (owner_login.create_owner): the owner
sets up the keys first, so nothing of theirs is ever written under a key the
machine holds by itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

DIR_ENV = "CSERIES_MACHINE_DIR"
STATE = "machine-state.json"
LOG = "access-log.jsonl"
RECORD = "unlock-record.jsonl"
TIMEOUT_S = 5.0
_MAX_LINE = 4096
_MAX_ROWS = 500


def machine_dir() -> str:
    return os.environ.get(DIR_ENV, "").strip()


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _read_lines(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    obj = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out[-_MAX_ROWS:]


def state() -> Optional[dict]:
    """machine-state.json, or None on a Host with no such machine (or before
    the machine has written it)."""
    d = machine_dir()
    return _read_json(os.path.join(d, STATE)) if d else None


def unlock_url(st: Optional[dict] = None) -> Optional[str]:
    st = st if st is not None else state()
    host = (st or {}).get("unlock_host")
    return f"https://{host}/" if isinstance(host, str) and host else None


def setup_blocked() -> Optional[str]:
    """The unlock page's address while this machine's vault has no keys of
    the owner's yet (setup waits for them), else None. A machine without a
    vault ("absent") and a Host with no machine never block."""
    d = machine_dir()
    if not d:
        return None
    st = state()
    if st is None:
        # The machine has not said yet: a cloud box always has, by the time
        # its owner can reach /setup (the vault's boot unit writes it), so
        # this is a box mid-restart. Refuse rather than guess.
        return "the machine's unlock page"
    vault = st.get("vault") or {}
    if vault.get("state") == "absent":
        return None
    if vault.get("enrolled") is True:
        return None
    return unlock_url(st) or "the machine's unlock page"


def access_log() -> list[dict]:
    d = machine_dir()
    return _read_lines(os.path.join(d, LOG)) if d else []


# Who a row's actor is, for a row that does not say more (the platform's rows
# from before detail.by, and the provider's and this machine's own). None of
# these claims a person: until a row says so, the platform's own tools could
# have been run by a person, a program or an AI agent alike.
ACTOR_LABELS = {
    "provisioner": "Privatae's provisioner (automated, no AI)",
    "breakglass": "Privatae, through its emergency tool",
    "server-login": "A sign-in to the server this machine runs on",
    "ovh-task": "The provider",
    "machine": "This machine",
}
# detail.by's kinds (the platform's access log: automated, ai, person).
BY_LABELS = {
    "automated": "A Privatae program, no AI ({name})",
    "ai": "An AI agent working for Privatae ({name})",
    "person": "A person at Privatae ({name})",
}
# A name the platform's access log takes (its BY_NAME_MAX and BY_NAME_RX):
# words only. A row whose name is anything else is read as a row without a
# by, so no text can pass for more of the label.
BY_NAME_MAX = 80
BY_NAME_RX = re.compile(r"[\w.,'\u2019()@:/+&-]+(?: [\w.,'\u2019()@:/+&-]+)*")


def actor_label(row: dict) -> str:
    """Who did what a row records, in words: from its detail.by when it has
    one, else from its actor. detail is text the row's hash covers, so a row
    is read here exactly as the machine checked it, and a detail that does
    not parse is just a row without a by."""
    try:
        by = json.loads(row.get("detail") or "{}").get("by")
    except (ValueError, TypeError, AttributeError):
        by = None
    name = by.get("name") if isinstance(by, dict) else None
    if isinstance(by, dict) and by.get("kind") in BY_LABELS and isinstance(name, str) \
            and len(name) <= BY_NAME_MAX and BY_NAME_RX.fullmatch(name):
        if by["kind"] == "person" and name.lower() == "a person at privatae":
            return "A person at Privatae"
        return BY_LABELS[by["kind"]].format(name=name)
    actor = str(row.get("actor"))
    return ACTOR_LABELS.get(actor, actor)


def unlock_record() -> list[dict]:
    d = machine_dir()
    return _read_lines(os.path.join(d, RECORD)) if d else []


class RestartError(Exception):
    """The restart was not asked for; the message is safe to show."""


async def restart(path: Optional[str] = None, timeout: float = TIMEOUT_S) -> int:
    """Ask the machine to restart (the door helper's machine.restart); the
    seconds until it does. Raises RestartError."""
    from server.network import door_helper
    path = path or door_helper.helper_path()
    if not path or not machine_dir():
        raise RestartError("this Host does not run on a machine it can restart")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=_MAX_LINE), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RestartError(f"the machine did not answer ({exc.__class__.__name__})") from None
    try:
        writer.write((json.dumps({"op": "machine.restart"}) + "\n").encode())
        await asyncio.wait_for(writer.drain(), timeout)
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.LimitOverrunError) as exc:
        raise RestartError(f"the machine did not answer ({exc.__class__.__name__})") from None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    try:
        answer = json.loads(line)
    except ValueError:
        raise RestartError("the machine's answer was not one line of JSON") from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        why = answer.get("error") if isinstance(answer, dict) else None
        raise RestartError(str(why)[:200] if why else "the machine refused")
    secs = answer.get("in_seconds")
    return secs if isinstance(secs, int) and not isinstance(secs, bool) and 0 <= secs < 600 else 5
