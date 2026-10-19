"""Model servers the owner names by address.

A bastion advertises the models its machine serves (model_backends). A
standalone Host may have no bastions at all, so the owner can also type the
address of a model server on their network — Ollama on the desktop, a
llama.cpp or vLLM server on another box. Each one is asked what it serves,
live, and its models join the same local model table the bastions feed.

An address is only ever asked two read-only questions: Ollama's
GET /api/tags, then the OpenAI-compatible GET /v1/models. A server that does
not answer within PROBE_TIMEOUT_S contributes no models, so a route to one of
its models reports "nothing on your network serves it" instead of dialing a
dead address. A bastion serving the same model is listed first and wins.

Stored in behavioral_params.local_endpoints as a list of base URLs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("cseries.owner_endpoints")

PARAM_KEY = "local_endpoints"
MAX_ENDPOINTS = 10
PROBE_TIMEOUT_S = 4.0
PROBE_CONNECT_S = 2.0
# How long one answer is reused. Short: the table follows what is up now.
CACHE_S = 30.0

_cache: dict[str, tuple[float, Optional[dict]]] = {}


class EndpointError(ValueError):
    """An address that cannot be stored; the message is safe to show."""


def normalize(url: str) -> str:
    """The stored form of an address: scheme://host[:port][/path], with no
    credentials, query, fragment, trailing slash or trailing /v1."""
    raw = (url or "").strip()
    try:
        u = urlsplit(raw)
        _ = u.port  # raises on a bad port
    except ValueError:
        raise EndpointError(f"{raw!r} is not a valid address")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise EndpointError(f"{raw!r} must start with http:// or https:// and name a host")
    if u.username or u.password:
        raise EndpointError("put no user name or password in the address")
    if u.query or u.fragment:
        raise EndpointError("the address takes no ?query or #fragment")
    path = u.path.rstrip("/")
    if path.lower().endswith("/v1"):
        path = path[:-3]
    return urlunsplit((u.scheme, u.netloc, path, "", ""))


def label(url: str) -> str:
    """How a server is named in the table: host:port."""
    u = urlsplit(url)
    host = f"[{u.hostname}]" if ":" in (u.hostname or "") else (u.hostname or "")
    port = u.port or (443 if u.scheme == "https" else 80)
    return f"{host}:{port}"


async def configured(pool, entity_id: int) -> list[str]:
    raw = await pool.fetchval(
        "SELECT behavioral_params->$2 FROM entity_registry WHERE id = $1",
        entity_id, PARAM_KEY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return [u for u in (raw or []) if isinstance(u, str)]


async def save(pool, entity_id: int, urls: list[str]) -> list[str]:
    """Validate and store the owner's list. Returns what was stored."""
    if not isinstance(urls, list):
        raise EndpointError("send a list of addresses")
    out: list[str] = []
    for u in urls:
        n = normalize(str(u))
        if n not in out:
            out.append(n)
    if len(out) > MAX_ENDPOINTS:
        raise EndpointError(f"at most {MAX_ENDPOINTS} model servers")
    await pool.execute(
        "UPDATE entity_registry SET behavioral_params = "
        "jsonb_set(COALESCE(behavioral_params, '{}'::jsonb), ARRAY[$2::text], $3::jsonb) "
        "WHERE id = $1", entity_id, PARAM_KEY, json.dumps(out))
    for u in out:
        _cache.pop(u, None)
    return out


async def _get_json(session, url: str):
    async with session.get(url) as resp:
        if resp.status != 200:
            return None
        return await resp.json(content_type=None)


async def probe(url: str) -> Optional[dict]:
    """{kind, models} for a server that answers, else None. Cached briefly."""
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    import aiohttp
    result = None
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S, connect=PROBE_CONNECT_S,
                                    sock_connect=PROBE_CONNECT_S)
    try:
        # trust_env=False: these are addresses on the owner's own network,
        # dialed directly, never through a proxy from the environment.
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as s:
            try:
                tags = await _get_json(s, url + "/api/tags")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                tags = None
            names = [m.get("name") or m.get("model") for m in (tags or {}).get("models", [])
                     if isinstance(m, dict)] if isinstance(tags, dict) else []
            names = [n for n in names if isinstance(n, str) and n]
            if names:
                result = {"kind": "ollama", "models": names}
            else:
                try:
                    listed = await _get_json(s, url + "/v1/models")
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    listed = None
                ids = [m.get("id") for m in (listed or {}).get("data", [])
                       if isinstance(m, dict)] if isinstance(listed, dict) else []
                ids = [i for i in ids if isinstance(i, str) and i]
                if ids:
                    result = {"kind": "llm-openai", "models": ids}
    except Exception as e:  # noqa: BLE001 — an unreachable server lists nothing
        log.debug("model server %s did not answer: %s", url, type(e).__name__)
        result = None
    _cache[url] = (now, result)
    return result


async def models(pool, entity_id: int) -> list[dict]:
    """The owner's servers' models, in the local model table's shape:
    [{id, bastion, endpoint, kind}] — `bastion` names the server (host:port)."""
    urls = await configured(pool, entity_id)
    answers = await asyncio.gather(*(probe(u) for u in urls))
    out: list[dict] = []
    for url, ans in zip(urls, answers):
        if not ans:
            continue
        for mid in ans["models"]:
            out.append({"id": mid, "bastion": label(url), "endpoint": url,
                        "kind": ans["kind"], "source": "owner"})
    return out
