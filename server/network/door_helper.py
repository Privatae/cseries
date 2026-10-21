"""The pairing window on a Host with a server of its own.

There the server's firewall keeps the enroll port closed. When the owner
pairs a machine (POST /api/network/bastions/setup, after a fresh passkey
check), a helper that runs as root on the server opens the port to the
owner's address for the pairing window and closes it after. The Host
reaches the helper on a unix socket, CSERIES_DOOR_HELPER: one JSON line out,
one JSON line back.

    {"op": "pairing.open", "addr": "<ip>"}  ->  {"ok": true, "expires_in": 900}
                                                {"ok": false, "error": "<why>"}

The helper checks who is calling, takes only a public unicast address and
limits how often it opens. The address is checked here as well, so a refusal
for a private address says so instead of reaching the helper.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os

HELPER_ENV = "CSERIES_DOOR_HELPER"
TIMEOUT_S = 5.0
_MAX_LINE = 4096
# The longest window an answer may claim, so a wrong answer cannot be shown
# to the owner as hours of open port.
_MAX_WINDOW_S = 3600


class HelperError(Exception):
    """The window did not open; the message is safe to show."""


def helper_path() -> str:
    return os.environ.get(HELPER_ENV, "").strip()


def public_address(addr: str | None) -> str | None:
    """`addr` in its plain form (an IPv4-mapped IPv6 address as IPv4) when
    it is a public unicast address, else None."""
    try:
        a = ipaddress.ip_address((addr or "").strip())
    except ValueError:
        return None
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    if not a.is_global or a.is_multicast:
        return None
    return str(a)


async def open_pairing(addr: str, path: str | None = None, timeout: float = TIMEOUT_S) -> int:
    """Ask the helper to open the enroll port to `addr` (a public address,
    see public_address); the seconds it stays open. Raises HelperError."""
    path = path or helper_path()
    if not path:
        raise HelperError("this Host has no pairing helper")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=_MAX_LINE), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise HelperError(f"the pairing helper did not answer ({exc.__class__.__name__})") from None
    try:
        writer.write((json.dumps({"op": "pairing.open", "addr": addr}) + "\n").encode())
        await asyncio.wait_for(writer.drain(), timeout)
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.LimitOverrunError) as exc:
        raise HelperError(f"the pairing helper did not answer ({exc.__class__.__name__})") from None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    try:
        answer = json.loads(line)
    except ValueError:
        raise HelperError("the pairing helper's answer was not one line of JSON") from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        why = answer.get("error") if isinstance(answer, dict) else None
        raise HelperError(str(why)[:200] if why else "the pairing helper refused")
    expires = answer.get("expires_in")
    if isinstance(expires, bool) or not isinstance(expires, int) or not 0 < expires <= _MAX_WINDOW_S:
        raise HelperError("the pairing helper's answer named no window")
    return expires
