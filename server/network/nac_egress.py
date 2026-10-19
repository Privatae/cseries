"""
NAC egress (P6) — the USER's outbound layer: one routing point + a signed policy.

The host's third-party web traffic (OpenRouter/BYOK, API calls, general internet) flows
through ONE in-pod point — this gate — which checks the user's SIGNED egress policy and
then forwards the request to a bastion. The BASTION (the user's own machine) does the
actual dial-out, over the USER's own internet connection (server/network/nac_egress.py is
the gate; client-go/egress.go on the bastion is the exit). Privatae's own lane (inference
+ its services, over the bridge) is untouched.

Split of duties, so neither half is the wrong place:
  - the pod GATES (host-local signed policy; whoever signed a rule is its authority),
  - the bastion EXITS (out the user's connection; the platform never sees the destination).

Two hard properties:
  - DEFAULT OPEN. No rule => the host reaches the whole internet (through its bastion). The
    user opts in to limit. A rule that fails its signature is SKIPPED — default-open means
    a tamper can only widen, never cut the host off.
  - NEVER LOGS DESTINATIONS. The gate reads the hostname only to allow/deny; it records
    nothing about where the host went (sovereignty). The bastion doesn't log either.
"""
import asyncio
import base64
import datetime as _dt
import json
import os
import sys
import time
from typing import NamedTuple, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Reuse the EXACT canonical form + pubkey accessor the user-gate signs with (P4), so an
# egress rule verifies the same way the inbound policy does.
from server.network.nac_stepup import _canonical, entity_verify_pubkey

EGRESS_PROXY_HOST = "127.0.0.1"
EGRESS_PROXY_PORT = int(os.environ.get("NAC_EGRESS_PORT", "13128"))
_RULES_TTL_SECONDS = 5.0          # re-read the policy from the pod DB at most this often
_HEAD_LIMIT = 65536
_IO_TIMEOUT = 20.0


# ── policy ────────────────────────────────────────────────────────────────

def _host_matches(pattern: str, host: str) -> bool:
    """A rule pattern matches a destination host. `*` matches everything (the catch-all
    for an allowlist); `*.suffix` matches the suffix and its subdomains; otherwise exact."""
    pattern = (pattern or "").lower().strip()
    host = (host or "").lower().strip()
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        bare = pattern[2:]
        return host == bare or host.endswith("." + bare)
    return host == pattern


#: What a caller is, when nothing names it. The entity pod's own network namespace —
#: kernel, hub and postgres share it, so loopback genuinely cannot tell them apart.
SUBJECT_POD = "pod"
#: The exit every rule has unless the user pinned one: a free bastion worker if any,
#: else the platform path. Exactly v059's behaviour.
EXIT_DEFAULT = "default"
#: Force the platform path even when a bastion is connected — the user-expressible
#: version of what _is_platform_dest does unconditionally for the platform's own hosts.
EXIT_PLATFORM = "platform"
#: 'bastion:<host_name>'. The name is the bastion's HEARTBEAT HOSTNAME, resolved to a
#: live token_id at dispatch time — never a token_id in the rule, because every
#: re-enrolment mints a new one (bastion_enroll) and the rule's terms are immutable.
EXIT_BASTION_PREFIX = "bastion:"

#: The app lane: a second listener, off-loopback, for callers in OTHER pods (app-runtime
#: pods, harness pods — the Browser is one). 13128 is loopback-only and cannot serve
#: them; it also carries the platform fallback, which a pinned request must never reach.
EGRESS_LANE_PORT = int(os.environ.get("NAC_EGRESS_LANE_PORT", "13129"))

#: How recently a bastion must have checked in to count as a live claimant of a
#: pinned hostname. Comfortably longer than the client's heartbeat cadence, short
#: enough that a machine replaced weeks ago is not still arguing over the name.
#: A timedelta, NOT "30 minutes": asyncpg binds `$n::interval` only from a
#: timedelta, and the string raised DataError, so every pinned rule failed its
#: lookup with "Could not look up the machine this rule is pinned to"
#: (measured 2026-09-22 against PG 18).
_PIN_LIVE_WINDOW = _dt.timedelta(minutes=30)


class EgressDecision(NamedTuple):
    """What the signed policy says about one request.

    `exit` is only meaningful when `allowed`; a deny carries EXIT_DEFAULT because
    propose-time validation refuses an exit on a deny (nac_propose.propose_egress).
    """
    allowed: bool
    exit: str = EXIT_DEFAULT
    #: id of the rule that decided, or None when nothing matched (default-open).
    rule_id: Optional[int] = None

    def __bool__(self) -> bool:
        # Kept truthy-compatible with the pre-v127 `-> bool` contract so a caller that
        # only asks "may this go out" reads the same either way.
        return self.allowed


def _subject_matches(rule_subject: str, caller: str) -> bool:
    """A rule's subject matches the calling identity. `*` matches every caller;
    otherwise it is an exact match on the subject string ('pod', 'app:<id>',
    'harness:<id>').

    Deliberately NOT a prefix or hierarchy match: 'app:' must not match every app, and
    a subject the user cannot read off the rule row as-written is a subject they cannot
    audit. Unknown/empty rule subjects are treated as `*` so a pre-v127 row — which has
    no subject in its signed terms — keeps applying to everything, which is what it has
    always meant."""
    rs = (rule_subject or "*").strip()
    return rs == "*" or rs == (caller or "").strip()


def egress_decision(rules: list, host: str,
                    subject: str = SUBJECT_POD) -> EgressDecision:
    """DEFAULT OPEN. Rules are pre-sorted (priority, id); first match wins. No match =>
    allowed, default exit. To build an allowlist, the user adds explicit allows + a
    final `*` deny.

    A rule matches when its host pattern matches AND its subject matches the caller.
    There is deliberately no specificity ranking — ordering stays exactly (priority, id)
    so the list the user reads in the Network app is the list the gate evaluates, in
    that order. The UI gives a subject-specific rule a lower default priority so it
    lands above the catch-alls visibly, rather than by a hidden precedence rule.

    Returns EgressDecision, which is truthy iff allowed — so the pre-v127 shape
    `if not egress_decision(rules, host):` still reads correctly."""
    for r in rules:
        if not _subject_matches(r.get("subject", "*"), subject):
            continue
        if _host_matches(r["pattern"], host):
            allowed = r["action"] == "allow"
            return EgressDecision(
                allowed=allowed,
                exit=(r.get("exit") or EXIT_DEFAULT) if allowed else EXIT_DEFAULT,
                rule_id=r.get("id"))
    return EgressDecision(True, EXIT_DEFAULT, None)


def _verify_rule(pubkey: bytes, terms: dict, sig_b64: str) -> bool:
    try:
        pub = pubkey if isinstance(pubkey, Ed25519PublicKey) else Ed25519PublicKey.from_public_bytes(pubkey)
        pub.verify(base64.b64decode(sig_b64), _canonical(terms))
        return True
    except Exception:
        return False


async def load_egress_rules(pool, verify_pubkey: Optional[bytes] = None) -> list:
    """Approved + non-revoked egress rules, signature-checked. The user-gate SIGNS every rule
    it approves (allow AND deny), so when the gate is ARMED (verify_pubkey present) a rule is
    authoritative ONLY if it carries a signature that verifies:
      - an approved-but-UNSIGNED rule is an anomaly — a direct DB write that bypassed the
        user-gate (the host setting the USER's egress policy without the user's signature) —
        and is skipped: "signed policy = authority", and the host is not the user.
      - a rule whose signature FAILS (a post-approval mutation) is skipped too.
    Default-open means skipping a rule can only WIDEN, never cut the host off, so a forged
    TIGHTEN can never take effect.

    C16 fail-closed-on-unsigned: when UNARMED (no entity verify key — boot race OR a key that
    won't load) we NO LONGER trust the stored columns of an UNSIGNED row. A signed_terms+signature
    row is still applied only if it verifies; an unsigned row is SKIPPED. "Signed policy = the
    user's authority" holds whether or not the key is loaded yet — an unsigned row is a direct DB
    write the user never signed, and the host is not the user. Because egress is default-OPEN,
    skipping unsigned rows while unarmed can only WIDEN (the host keeps reaching the internet),
    so this never cuts the host off — it just refuses to let an unsigned row set the USER's policy.
    The proxy ALSO re-arms (EgressProxy._rules_now retries the key), so this unarmed window closes
    as soon as the key loads."""
    rows = await pool.fetch(
        "SELECT id, pattern, action, priority, signed_terms, signature "
        "FROM nac_egress WHERE approved_at IS NOT NULL AND revoked_at IS NULL "
        "ORDER BY priority ASC, id ASC")
    out = []
    for row in rows:
        pattern, action, priority = row["pattern"], row["action"], row["priority"]
        st, sig = row["signed_terms"], row["signature"]
        if st is None or sig is None:
            # Unsigned approved row — not user-authorized. Skipped whether armed or not.
            sys.stderr.write(f"NAC egress: approved rule {row['id']} is unsigned "
                             f"({'armed' if verify_pubkey is not None else 'unarmed'} gate) — "
                             f"skipping (not user-authorized)\n")
            continue
        if verify_pubkey is not None:
            terms = st if isinstance(st, dict) else json.loads(st)
            if not _verify_rule(verify_pubkey, terms, sig):
                sys.stderr.write(f"NAC egress: rule {row['id']} failed signature verification — skipping\n")
                continue
        else:
            # Unarmed but the row IS signed: we cannot verify the signature without the key, so we
            # cannot honor a signed TIGHTEN we can't authenticate. Skip (default-open WIDENs safely).
            sys.stderr.write(f"NAC egress: rule {row['id']} signed but gate unarmed (no verify key) "
                             f"— skipping until the key loads\n")
            continue
        terms = st if isinstance(st, dict) else json.loads(st)
        pattern = terms.get("pattern", pattern)
        action = terms.get("action", action)
        priority = terms.get("priority", priority)
        # v127 subject/exit: read from the VERIFIED TERMS with CONSTANT defaults —
        # never falling back to the row column the way the three lines above do.
        # Those fall back for historical reasons; copying that here would let an
        # unsigned direct write to the `exit` column re-route the user's traffic,
        # which is the exact thing the signature exists to prevent. A pre-v127 row
        # has neither key in its terms and correctly loads as "any subject, default
        # exit" — what it has always meant.
        subject = terms.get("subject", "*")
        exit_ = terms.get("exit", EXIT_DEFAULT)
        out.append({"id": row["id"], "pattern": pattern, "action": action,
                    "priority": priority, "subject": subject, "exit": exit_})
    out.sort(key=lambda r: (r["priority"], r["id"]))
    return out


# ── platform egress upstream (the node proxy) ───────────────────────────────

def _upstream_proxy() -> Optional[tuple]:
    """The node-level egress proxy from the pod env (HTTP(S)_PROXY), if set.

    Entity pods are IPv6-only; the node proxy is the platform's IPv4-capable
    exit. Direct dispatch chains through it so an IPv4-only destination (e.g.
    the platform's own gitea) stays reachable when no bastion is connected —
    a raw dial from the pod can never reach an A-only host."""
    raw = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
           or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
    if not raw:
        return None
    rest = raw.split("://", 1)[1] if "://" in raw else raw
    rest = rest.split("/", 1)[0]
    host, port = EgressProxy._host_port(rest, 3128)
    if not host:
        return None
    return host, port


def _no_proxy_match(host: str) -> bool:
    """True when the pod env's NO_PROXY says this destination bypasses the
    node proxy (loopback / cluster-internal names — those dial direct)."""
    host = (host or "").lower().strip()
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    for entry in raw.split(","):
        entry = entry.lower().strip()
        if not entry:
            continue
        if host == entry:
            return True
        suffix = entry if entry.startswith(".") else "." + entry
        if host.endswith(suffix):
            return True
    return False


# ── platform-owned destinations: always the platform path, never the bastion ─

def _platform_hosts() -> set:
    """Hosts that are the PLATFORM's own services, which the host reaches as control
    traffic (its gitea identity, per-run token mints, the Gitea app, the editor's git).
    These never ride the user's bastion: the bastion exit is the USER's sovereign
    egress for the host's third-party traffic, and the platform forge is reachable
    only from platform addresses — a request that left through the user's home
    connection is dropped at the edge. Measured before this fix: a host with a
    connected egress bastion reached gitea from the user's home IP.

    Source of truth is the pod env the operator injects (GITEA_URL; unset means
    no platform forge); PLATFORM_DIRECT_HOSTS (comma-separated) extends it."""
    out = set()
    raw = (os.environ.get("GITEA_URL") or "").strip()
    host = raw.split("://", 1)[-1].split("/", 1)[0]
    host = EgressProxy._host_port(host, 443)[0].lower()
    if host:
        out.add(host)
    for extra in (os.environ.get("PLATFORM_DIRECT_HOSTS") or "").split(","):
        extra = extra.strip().lower()
        if extra:
            out.add(extra)
    return out


def _is_platform_dest(host: Optional[str]) -> bool:
    return bool(host) and host.lower().strip() in _platform_hosts()


# ── the app-lane credential, and keeping it out of the forwarded head ───────

def _split_head(head: bytes):
    """(header_bytes, separator, remainder). The read loop stops at the end of the
    headers but may have pulled body bytes with them, so anything that edits headers
    must not touch what follows."""
    for sep in (b"\r\n\r\n", b"\n\n"):
        i = head.find(sep)
        if i >= 0:
            return head[:i], sep, head[i + len(sep):]
    return head, b"", b""


def strip_proxy_auth(head: bytes) -> bytes:
    """Remove every Proxy-Authorization header from a buffered request head.

    MUST run before the head is forwarded anywhere, on every path, because the head
    goes out verbatim in all of them: to the bastion (egress_relay._relay), to the node
    squid, and — on absolute-form plain HTTP — the BASTION then writes it straight to
    the destination origin (client-go/egress.go:88 `upstream.Write(head.String())`).
    An unstripped credential on that path is handed to an arbitrary third-party server,
    and this one buys the use of the user's own home address.

    On CONNECT the bastion discards the head and writes its own 200, so the credential
    would stop there — but squid still sees it on the platform path. Stripping once
    here, before the CONNECT/plain branch, covers every case with no per-path reasoning.
    """
    header, sep, rest = _split_head(head)
    if b"proxy-authorization" not in header.lower():
        return head
    # Split on the line ending this head actually uses. _split_head accepts a
    # bare-LF terminator, so assuming CRLF here would leave the credential in a
    # mixed-ending head untouched — and "we stripped it" has to be true of every
    # head we forward, not of the well-formed ones.
    eol = b"\r\n" if b"\r\n" in header else b"\n"
    kept = [ln for ln in header.split(eol)
            if not ln[:20].lower().startswith(b"proxy-authorization:")]
    return eol.join(kept) + sep + rest


#: A caller that sets this header can route a request itself if the gate tells it
#: no rule named the destination. Only the harness chooser sets it; the Browser
#: does not, so the Browser keeps the unconditional forward it has always had.
FALLBACK_HEADER = b"x-cseries-egress-fallback"


def wants_direct_fallback(head: bytes) -> bool:
    """True iff the caller advertised that it can reach the destination itself.

    The header is an OPT-IN, and it has to be: `_direct_dispatch` on an unnamed
    destination is what every app-lane caller got before the harness chooser
    existed. Answering the sentinel to a caller that cannot act on it would break
    the Browser, which shares this lane.
    """
    header, _, _ = _split_head(head)
    for ln in header.split(b"\r\n"):
        if ln[:len(FALLBACK_HEADER)].lower().startswith(FALLBACK_HEADER):
            return ln.split(b":", 1)[1].strip() == b"1"
    return False


def strip_fallback_header(head: bytes) -> bytes:
    """Drop the advertisement before the head is forwarded anywhere. Same reason
    strip_proxy_auth exists: on absolute-form HTTP the bastion writes this head
    straight to a third-party origin, and our routing hints are not theirs."""
    header, sep, rest = _split_head(head)
    if FALLBACK_HEADER not in header.lower():
        return head
    eol = b"\r\n" if b"\r\n" in header else b"\n"
    kept = [ln for ln in header.split(eol)
            if not ln[:len(FALLBACK_HEADER)].lower().startswith(FALLBACK_HEADER)]
    return eol.join(kept) + sep + rest


def _proxy_basic_subject(head: bytes) -> tuple:
    """(subject, token) from `Proxy-Authorization: Basic base64(subject:token)`, or
    (None, None).

    BASIC, not Bearer, and that is forced by the client: Chromium is what has to
    authenticate to this lane and it has no flag for a proxy credential — it answers a
    407 challenge with Basic and nothing else. Playwright's
    chromium.launch(proxy={server, username, password}) is the browser-side half."""
    header, _, _ = _split_head(head)
    for ln in header.split(b"\r\n"):
        if not ln[:20].lower().startswith(b"proxy-authorization:"):
            continue
        val = ln.split(b":", 1)[1].strip()
        if not val[:6].lower().startswith(b"basic"):
            continue
        try:
            raw = base64.b64decode(val[5:].strip()).decode("utf-8", "replace")
        except Exception:
            return None, None
        # SPLIT FROM THE RIGHT. A subject contains a colon ('harness:browser',
        # 'app:example') and RFC 7617 gives no way to escape one in the userid, so
        # partitioning on the FIRST colon yields subject='harness' and hands the rest of
        # the real subject to the token — which then never verifies. The token is a
        # sha256 hexdigest and can never contain a colon, so the LAST one is always the
        # separator. Chromium builds the field as username + ":" + password, so
        # username='harness:browser' round-trips exactly.
        subject, _, token = raw.rpartition(":")
        return (subject.strip() or None), (token.strip() or None)
    return None, None


# ── the in-pod gate (forwards allowed traffic to the bastion exit) ──────────

class EgressProxy:
    def __init__(self, pool, entity_id: int, verify_pubkey: Optional[bytes],
                 app_lane: bool = False):
        self.pool = pool
        self.entity_id = entity_id
        self.verify_pubkey = verify_pubkey
        #: False = the loopback gate (subject is always 'pod'). True = the off-loopback
        #: app lane: every caller presents a per-subject credential, and there is no
        #: implicit identity to fall back on.
        self.app_lane = app_lane
        self._rules: list = []
        self._rules_at = 0.0
        self._server = None
        #: The kernel's app-token secret, loaded once (see _lane_subject_ok).
        self._app_secret = None

    @staticmethod
    async def _refuse(writer, status: bytes, body: bytes, extra: bytes = b"") -> None:
        """One-shot refusal carrying a body that says what happened. Never a bare close
        and never a bodyless status: "no silent failures" is the standing rule on this
        path, and the reader of these bodies is a person looking at a page that did not
        load."""
        try:
            writer.write(b"HTTP/1.1 " + status + b"\r\n"
                         b"Content-Type: text/plain; charset=utf-8\r\n" + extra +
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                         % (len(body), body))
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass

    async def _lane_subject_ok(self, subject: Optional[str], token: Optional[str]) -> bool:
        """Is this a real subject presenting its own credential?

        '*' and 'pod' are refused outright: 'pod' is what loopback MEANS and is not
        claimable from off-pod, and '*' is a rule-matching wildcard, never an identity.
        Without that, an off-pod caller could assert the subject the loopback gate uses
        and pick up rules written for the kernel.

        The token is checked with harness_backend.check_egress_subject_token — derived,
        not stored, and under a domain prefix distinct from app_source_token's so a
        leaked source token cannot be replayed here."""
        if not subject or not token:
            return False
        if subject in ("*", SUBJECT_POD):
            return False
        try:
            import hashlib
            import hmac
            import secrets as _s
            # CACHED. tokens.get_secret runs CREATE TABLE IF NOT EXISTS + a SELECT
            # on every call; on the loopback gate that never happens, but here it
            # would run once per CONNECT, and a page load is dozens of concurrent
            # CONNECTs against a brain pool of max_size=10 shared with the whole
            # kernel. The secret is immutable by construction (INSERT ... ON
            # CONFLICT DO NOTHING), so caching it for the process is safe.
            if self._app_secret is None:
                from server.applications import tokens
                self._app_secret = await tokens.get_secret(self.pool)
            want = hmac.new(self._app_secret,
                            f"nac-egress-subject:{subject}".encode("utf-8"),
                            hashlib.sha256).hexdigest()
            return _s.compare_digest(want.encode("utf-8"), token.encode("utf-8"))
        except Exception:
            return False

    async def _resolve_pin(self, name: str) -> tuple:
        """'bastion:<host_name>' -> (token_id, None), or (None, why_bytes).

        Uses nac_commit.resolve_bastion, which scopes to THIS entity and to
        operator_kind='bastion' AND revoked_at IS NULL — unlike embed_relay's inline
        hostname lookup, which does neither and so can resolve a revoked token and then
        fail closed on a machine the user believes is live.

        AMBIGUITY IS REFUSED, never resolved. The heartbeat hostname is self-reported
        and carries no uniqueness constraint, and resolve_bastion's hostname branch
        takes the latest claimant. For a PIN that is the wrong rule: another of the
        user's own machines could take over their bank traffic just by booting with the
        same OS hostname. topology already refuses to route on a claimed hostname; this
        is the pin-time version of the same judgement."""
        if not name:
            return None, b"This rule pins an exit but names no machine."
        try:
            from server.network import nac_commit
            # LIVE claimants only, and getting this filter wrong breaks the
            # feature's headline case. Re-enrolment MINTS A NEW TOKEN and does not
            # revoke the old one (bastion_enroll.py has no revoke; the only
            # revocation is the user's explicit remove in topology.revoke_bastion),
            # and the heartbeat table is one row PER TOKEN keyed on token_id
            # (v056/v057), so the dead token's row keeps claiming the hostname
            # forever. Counting on revoked_at alone therefore reports "two of your
            # machines" for a user with one, permanently, the first time their
            # laptop comes back after the 7-day bearer expiry — which is precisely
            # the fail-closed-forever outcome naming the host instead of the
            # token_id was chosen to avoid.
            #
            # So: the bearer must still be valid AND the machine must have checked
            # in recently. A pinned machine that is merely OFFLINE counts zero here
            # and resolves fine, then fails at has_bastion with "not connected" —
            # which is the true reason and the actionable message.
            n = await self.pool.fetchval(
                "SELECT count(DISTINCT h.token_id) FROM network_bastion_heartbeat h "
                "JOIN mcp_client_tokens t ON t.id = h.token_id "
                "WHERE h.hostname = $1 AND t.entity_id = $2 "
                "AND t.operator_kind = 'bastion' AND t.revoked_at IS NULL "
                "AND (t.expires_at IS NULL OR t.expires_at > now()) "
                "AND h.last_seen_at > now() - $3::interval",
                name, self.entity_id, _PIN_LIVE_WINDOW)
            if n and int(n) > 1:
                return None, (b"The name '" + name.encode("utf-8", "replace") +
                              b"' matches more than one of your machines, so this pinned "
                              b"rule cannot say which. Rename one, or repin the rule in "
                              b"your Network app.")
            found = await nac_commit.resolve_bastion(self.pool, self.entity_id, server=name)
        except Exception:
            return None, (b"Could not look up the machine this rule is pinned to. "
                          b"Nothing was sent another way.")
        if not found:
            return None, (b"No enrolled machine of yours is called '" +
                          name.encode("utf-8", "replace") + b"'. It may have been removed, "
                          b"or re-enrolled under a different name; repin the rule in your "
                          b"Network app.")
        return int(found["token_id"]), None

    async def _rearm_if_needed(self) -> None:
        """C16 re-arm: if the verify key was absent at boot (it could not load yet), retry it on
        each refresh. Once it loads the gate becomes ARMED and signed rules are honored; until then
        load_egress_rules skips unsigned/unverifiable rows (fail-closed-on-unsigned). This closes
        the permanent-unarmed window — without this the boot-time None would stick forever."""
        if self.verify_pubkey is not None:
            return
        try:
            self.verify_pubkey = await entity_verify_pubkey(self.pool, self.entity_id)
            sys.stderr.write("NAC egress: verify key recovered — gate is now ARMED\n")
        except Exception:
            self.verify_pubkey = None  # still not loadable; stay fail-closed-on-unsigned

    async def _rules_now(self) -> list:
        now = time.monotonic()
        if now - self._rules_at > _RULES_TTL_SECONDS:
            await self._rearm_if_needed()
            try:
                self._rules = await load_egress_rules(self.pool, self.verify_pubkey)
                self._rules_at = now
            except Exception:
                self._rules_at = now  # keep last-known; never fail the host's internet shut
        return self._rules

    @staticmethod
    def _host_port(target: str, default_port: int):
        target = target.strip()
        if target.startswith("["):
            host, _, rest = target[1:].partition("]")
            port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
            return host, port
        if target.count(":") == 1:
            host, _, p = target.partition(":")
            return host, (int(p) if p.isdigit() else default_port)
        return target, default_port

    def _dest_host(self, method: str, target: str, head: bytes) -> Optional[str]:
        """The destination host the policy is checked against."""
        if method == "CONNECT":
            return self._host_port(target, 443)[0]
        if "://" in target:
            rest = target.split("://", 1)[1]
            return self._host_port(rest.split("/", 1)[0], 80)[0]
        for ln in head.split(b"\r\n"):
            if ln[:5].lower() == b"host:":
                return self._host_port(ln.split(b":", 1)[1].strip().decode("latin-1", "ignore"), 80)[0]
        return None

    async def _handle(self, reader, writer):
        try:
            head = b""
            while b"\r\n\r\n" not in head and b"\n\n" not in head:
                chunk = await asyncio.wait_for(reader.read(2048), timeout=_IO_TIMEOUT)
                if not chunk:
                    break
                head += chunk
                if len(head) > _HEAD_LIMIT:
                    break
            if not head:
                writer.close()
                return
            first, _, _ = head.partition(b"\r\n")
            parts = first.split()
            method = parts[0].decode("latin-1", "ignore").upper() if parts else ""
            target = parts[1].decode("latin-1", "ignore") if len(parts) > 1 else ""
            host = self._dest_host(method, target, head)

            from server.network.egress_relay import get_egress_relay, log as egress_log

            # WHO IS CALLING. On the loopback gate: the entity pod's own netns, and it
            # cannot be anything else — kernel, hub and postgres share that namespace, so
            # the subject is 'pod' and does not pretend to name one process. On the app
            # lane: whoever holds a valid per-subject credential.
            subject = SUBJECT_POD
            if self.app_lane:
                subject, token = _proxy_basic_subject(head)
                if not await self._lane_subject_ok(subject, token):
                    await self._refuse(
                        writer, b"407 Proxy Authentication Required",
                        b"This lane needs your app or harness credential.",
                        extra=b'Proxy-Authenticate: Basic realm="cseries-egress"\r\n')
                    return
            # STRIP THE CREDENTIAL before the head can reach anything downstream. Every
            # onward path forwards it verbatim, and on absolute-form HTTP the bastion
            # hands it to the destination origin (client-go/egress.go:88).
            can_self_route = wants_direct_fallback(head)
            head = strip_fallback_header(strip_proxy_auth(head))

            # GATE: the user's signed policy. Default-open; a deny tightens. NB: we log the
            # DECISION only, never the destination host — the gate "records nothing about where
            # the host went" (sovereignty), and pod logs are platform-readable.
            #
            # No parsable destination => no gate, and the DEFAULT exit. That is v059's
            # behaviour (`if host and not egress_decision(...)`) and it is preserved
            # deliberately: `_host_matches("*", None)` is True, so evaluating the policy
            # here would start refusing host-less requests that pass today. Tightening
            # that is a separate change, not something to smuggle into this one.
            decision = (egress_decision(await self._rules_now(), host, subject)
                        if host else EgressDecision(True, EXIT_DEFAULT, None))
            if not decision.allowed:
                egress_log.info("gate: request denied by signed policy")
                await self._refuse(
                    writer, b"403 Forbidden",
                    b"Blocked by your signed egress policy: this destination is on your "
                    b"deny list. Change the rule from your Network app or your agent to allow it.")
                return

            # EXIT. Which way out is NOT a policy decision — the signed gate above
            # already ran — it is a routing one.
            #
            # Platform-owned destinations (the platform's gitea) take the platform path
            # unconditionally and ahead of everything else: the platform forge is reachable
            # only from platform addresses, so a request that left through the user's
            # home connection is dropped at the edge. This floor must not depend on a
            # user rule existing.
            if _is_platform_dest(host):
                await self._direct_dispatch(head, reader, writer, method, target)
                return

            exit_ = decision.exit or EXIT_DEFAULT
            relay = get_egress_relay()

            # WHAT 'default' MEANS DEPENDS ON WHICH DOOR YOU CAME IN, and both
            # readings are the same rule: "unchanged from before this feature".
            #
            #   loopback gate -> any free bastion worker, else the platform path.
            #     That is v059's behaviour for the kernel's own traffic and it is
            #     what a host with a connected bastion already had.
            #   app lane      -> the PLATFORM PATH. A pod that never had this lane
            #     reached the internet through the node squid, so an unpinned
            #     caller must keep doing exactly that.
            #
            # Without this, the browser sidecar preferring the lane whenever the
            # env is set would silently move every user's browsing onto whatever
            # bastion happened to be connected — no rule, no step-up, no pin, and
            # all of it through this pod's proxy. "Nothing changes until a rule
            # names a subject or an exit" has to be true of the lane too, or it is
            # not true at all.
            if exit_ == EXIT_DEFAULT and self.app_lane:
                # NOTHING NAMED THIS DESTINATION, and the caller can route it
                # itself: say so and let it. A harness that sent every byte here
                # would put its whole build through the entity pod — the kernel's
                # event loop pumping it, and the entity node's ONE shared IPv4
                # carrying it against a per-pod cap sized for a single workload
                # (manifests/0012 `pod_conn_cap maxconn 64`, and the 2026-08-14
                # measurement behind it: 20 pods tripped a public 429 wall in
                # under 5s). Its own node proxy already has a bucket of its own.
                # Rule-named destinations still come here, which is the point of
                # the lane; only the unnamed ones go back where they were.
                # NOT gated on `rule_id is None`. An ALLOW with no pinned exit
                # returns a rule_id and EXIT_DEFAULT (egress_decision:133-136),
                # and the docstring names exactly that as how an allowlist is
                # built — "explicit allows + a final `*` deny". Gating on the
                # rule_id would therefore send an allowlist user's ENTIRE build
                # through this pod, which is the collapse the chooser exists to
                # avoid. Every policy decision still lands: a deny was refused
                # above (:615-621), and a pinned exit never reaches this block.
                # What is left is "allowed, nowhere in particular" — and for a
                # caller that can route itself, where that used to go is its own
                # node proxy.
                if can_self_route:
                    await self._refuse(
                        writer, b"421 Misdirected Request",
                        b"No signed egress rule names this destination, and you "
                        b"said you can reach it yourself. Use your own node proxy.")
                    return
                await self._direct_dispatch(head, reader, writer, method, target)
                return

            # PINNED: 'platform' — force the platform path even with a bastion up.
            if exit_ == EXIT_PLATFORM:
                await self._direct_dispatch(head, reader, writer, method, target)
                return

            # PINNED: 'bastion:<host_name>' — that machine or nothing. NEVER falls back
            # to another bastion and NEVER to the platform path. A request the user
            # pinned to their own machine has to fail loudly rather than quietly become
            # the thing they pinned it away from: leaving by the datacentre address is
            # the whole failure this exists to prevent.
            if exit_.startswith(EXIT_BASTION_PREFIX):
                name = exit_[len(EXIT_BASTION_PREFIX):].strip()
                token_id, why = await self._resolve_pin(name)
                if token_id is None:
                    egress_log.info("gate: pinned exit unresolved")
                    await self._refuse(writer, b"503 Service Unavailable", why)
                    return
                # "never connected" is a fast 503; "connected but busy" waits, because a
                # page load is many concurrent requests through one bastion.
                if not relay.has_bastion(token_id):
                    await self._refuse(
                        writer, b"503 Service Unavailable",
                        b"Your machine '" + name.encode("utf-8", "replace") +
                        b"' is not connected, and this traffic is pinned to it. "
                        b"Start its bastion, or change the rule in your Network app.")
                    return
                if await relay.dispatch(head, reader, writer, timeout=_IO_TIMEOUT,
                                        bastion_token_id=token_id):
                    return
                await self._refuse(
                    writer, b"503 Service Unavailable",
                    b"Your machine '" + name.encode("utf-8", "replace") +
                    b"' did not answer in time, and this traffic is pinned to it "
                    b"(it was not sent another way).")
                return

            # DEFAULT: bastion egress is OPT-IN. Any free worker if one exists, else the
            # platform egress path — the node proxy when configured, else a direct pod
            # dial (see _direct_dispatch). A missing bastion must NEVER cut the host off
            # or return a bare 502 — bastion egress is a user CHOICE, not a requirement
            # (there is no forced-bastion invariant). The worker_count guard is
            # load-bearing, not an optimisation: without it a host with no bastion
            # waits the full timeout on every request, and with no queue
            # ever created asyncio.wait(set()) raises into the outer handler, which
            # closes the socket with no body at all.
            if relay.worker_count() > 0:
                ok = await relay.dispatch(head, reader, writer, timeout=_IO_TIMEOUT)
                if ok:
                    return  # on success the worker owns + closes the client stream
                # A worker existed but went away mid-dispatch — fall through to direct egress
                # rather than fail the host (e.g. the bastion's WS dropped during a pod roll).
                egress_log.info("gate: bastion worker vanished mid-dispatch — direct-egress fallback")
            await self._direct_dispatch(head, reader, writer, method, target)
        except asyncio.TimeoutError:
            try:
                writer.close()
            except Exception:
                pass
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    async def _direct_dispatch(self, head, reader, writer, method, target):
        """Default/platform egress: used when no egress bastion is connected (bastion
        egress is opt-in, not required). The pod itself is IPv6-only, so the default
        route is a CHAIN through the node egress proxy (HTTP(S)_PROXY env) — it speaks
        the same proxy protocol this gate receives, so the buffered head forwards
        verbatim (CONNECT and absolute-form alike) and the node proxy reaches IPv4-only
        destinations a raw pod dial never can. NO_PROXY destinations (loopback /
        cluster-internal) and the no-upstream case keep the raw dial; a dead upstream
        falls back to it too. Never a silent 502: a real connect failure returns a body
        saying what happened."""
        from server.network.egress_relay import log as egress_log
        dhost = None
        try:
            if method == "CONNECT":
                dhost, dport = self._host_port(target, 443)
            elif "://" in target:
                rest = target.split("://", 1)[1]
                hostport, _, _ = rest.partition("/")
                dhost, dport = self._host_port(hostport, 80)
            else:
                dhost = self._dest_host(method, target, head)
                dport = 80
            if not dhost:
                raise ValueError("no destination host")
            upstream = None if _no_proxy_match(dhost) else _upstream_proxy()
            if upstream is not None:
                try:
                    remote_r, remote_w = await asyncio.wait_for(
                        asyncio.open_connection(upstream[0], upstream[1]),
                        timeout=_IO_TIMEOUT)
                except Exception as exc:
                    # Node proxy itself unreachable — raw dial is better than dead.
                    egress_log.info("gate: node egress proxy unreachable (%s) — "
                                    "direct-dial fallback", type(exc).__name__)
                    upstream = None
                else:
                    try:
                        # The client already speaks proxy protocol and so does the
                        # node proxy: forward the buffered request verbatim (the
                        # CONNECT reply comes from the node proxy) and splice.
                        remote_w.write(head)
                        await remote_w.drain()
                        await self._pipe(reader, writer, remote_r, remote_w)
                    except Exception:
                        for w in (writer, remote_w):
                            try:
                                w.close()
                            except Exception:
                                pass
                    return
            remote_r, remote_w = await asyncio.wait_for(
                asyncio.open_connection(dhost, dport), timeout=_IO_TIMEOUT)
        except Exception as exc:
            egress_log.info("gate: direct egress could not connect (%s)", type(exc).__name__)
            if _is_platform_dest(dhost):
                body = (b"Outbound connection failed: the platform egress proxy is unreachable, so "
                        b"this host cannot reach the platform's own service. A bastion cannot help "
                        b"here (platform services answer platform addresses only); retry shortly.")
            else:
                body = (b"Outbound connection failed: the destination could not be reached and no "
                        b"egress bastion is connected. Set up a bastion in the Network app to route "
                        b"through your own connection, or check that the destination is reachable.")
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n"
                             b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
                await writer.drain()
            except Exception:
                pass
            try:
                writer.close()
            except Exception:
                pass
            return
        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                remote_w.write(head)  # forward the buffered request; body/rest streams via the pipe
                await remote_w.drain()
            await self._pipe(reader, writer, remote_r, remote_w)
        except Exception:
            for w in (writer, remote_w):
                try:
                    w.close()
                except Exception:
                    pass

    @staticmethod
    async def _pipe(client_reader, client_writer, remote_reader, remote_writer):
        """Bidirectional splice client <-> remote until either side closes."""
        async def _one(src, dst):
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass
        await asyncio.gather(
            _one(client_reader, remote_writer),
            _one(remote_reader, client_writer),
        )

    async def start(self):
        await self._rules_now()
        if self.app_lane:
            # Off-loopback, because the callers are in OTHER pods (a harness pod can be
            # on another node entirely). `::` — entity pods are IPv6-only and the pod IP
            # is what a sibling pod dials; see server/loopback.py on why `::` and not a
            # v4 literal. Reachability is bounded by the CiliumNetworkPolicy, identity by
            # the per-subject credential; neither alone is the control.
            self._server = await asyncio.start_server(self._handle, "::", EGRESS_LANE_PORT)
        else:
            self._server = await asyncio.start_server(
                self._handle, EGRESS_PROXY_HOST, EGRESS_PROXY_PORT)
        return self._server

    async def stop(self):
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass


async def start_egress_proxy(pool, entity_id: int) -> "EgressProxy":
    """Build + start the in-pod egress gate. Arms tamper-verify with the host's entity
    public key (same key the user-gate signs with). Called once in server lifespan.

    C16: if the key can't load at boot the gate starts UNARMED — but unarmed no longer means
    "trust the stored columns". load_egress_rules skips unsigned/unverifiable rows while unarmed
    (fail-closed-on-unsigned), and EgressProxy._rules_now re-arms on each refresh, so the unarmed
    window closes the moment the key becomes loadable. A boot key-load failure therefore never
    silently honors a host's unsigned egress policy."""
    verify_pubkey = None
    try:
        verify_pubkey = await entity_verify_pubkey(pool, entity_id)
    except Exception as e:
        sys.stderr.write(f"NAC egress: verify key not loadable at boot — starting UNARMED "
                         f"(unsigned rows skipped; will re-arm): {e!r}\n")
        verify_pubkey = None
    proxy = EgressProxy(pool, entity_id, verify_pubkey)
    await proxy.start()
    return proxy


async def start_egress_lane(pool, entity_id: int) -> Optional["EgressProxy"]:
    """Start the APP LANE (:13129) beside the loopback gate — the same policy engine,
    reachable from the entity's OWN other pods (app runtimes, harnesses), where every
    caller must name itself with a credential.

    Why a second listener rather than opening 13128: 13128 is loopback-bound, and it
    carries the platform fallback that a pinned request must never reach. Keeping them
    separate means "which port did this arrive on" already answers "may this claim a
    subject", instead of that being a flag someone can get wrong later.

    Best-effort: the lane is inert until a rule names a subject, so a failure here must
    never stop the kernel booting. It is logged loudly, not swallowed."""
    verify_pubkey = None
    try:
        verify_pubkey = await entity_verify_pubkey(pool, entity_id)
    except Exception:
        verify_pubkey = None
    lane = EgressProxy(pool, entity_id, verify_pubkey, app_lane=True)
    try:
        await lane.start()
    except Exception as e:
        sys.stderr.write(f"NAC egress: app lane failed to bind :{EGRESS_LANE_PORT} — "
                         f"subject-pinned rules will not route ({e!r})\n")
        return None
    return lane
