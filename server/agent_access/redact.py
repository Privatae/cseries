"""Secrets out of a command line before an outside agent reads it.

The bastion reports scheduled jobs' commands and agents' command lines as it
found them (privatae-bastion client-go schedulers*.go, agents*.go), and
redacts them only where its own console shows them (uimachine.go
collectSchedules -> redactSecretArgs). A command line can hold a key
(`aider --api-key sk-...`), and an outside agent handed it would hold the key.
This is a port of client-go mcpconfig.go redactSecretArgs / redactValue /
redactURL, so what an agent reads on the Host is what the machine's own
console would show: the same patterns and the same marker.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

REDACTED = "<redacted>"
_HOST_MAX = 253

# client-go mcpconfig.go secretPrefixes: credentials whose issuers publish a
# fixed prefix.
_SECRET_PREFIXES = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:"
    r"sk-ant-[A-Za-z0-9_-]{16,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
    r"xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
    r"glpat-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{36}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
# secretNameRe: a variable, header or flag whose name says it holds a secret.
_SECRET_NAME = re.compile(r"(?i)(key|token|secret|passw|pwd|credential|auth|bearer|cookie|session)")


def _secret_name(name: str) -> bool:
    return bool(_SECRET_NAME.search(name or ""))


def _auth_header(name: str) -> bool:
    n = name.lower()
    return n in ("authorization", "cookie", "proxy-authorization") or _secret_name(n)


def _pure_reference(v: str) -> bool:
    """The whole value is one ${...} reference the client expands."""
    v = v.strip()
    return (v.startswith("${") and v.endswith("}") and v.count("${") == 1
            and v.index("}") == len(v) - 1)


def _redact_url(raw: str) -> str:
    """Scheme and host kept; userinfo, path, query and fragment replaced."""
    try:
        u = urlsplit(raw)
    except ValueError:
        return REDACTED
    userinfo, at, hostport = u.netloc.rpartition("@")
    if not hostport:
        return REDACTED
    out = u.scheme + "://"
    if at:
        out += REDACTED + "@"
    out += hostport[:_HOST_MAX]
    if u.path.strip("/") or u.query or u.fragment:
        out += "/" + REDACTED
    return out


def redact_value(name: str, v: str) -> str:
    """One value with whatever secret it carries replaced. `name` is the flag
    or variable it was given under, '' for a positional argument."""
    if v == "" or _pure_reference(v):
        return v
    if _SECRET_PREFIXES.search(v):
        return REDACTED
    if _secret_name(name):
        return REDACTED
    low = v.lower()
    if low.startswith("bearer ") or low.startswith("basic "):
        return REDACTED
    i = v.find("://")
    if i > 0 and not any(ch in v[:i] for ch in " =:"):
        return _redact_url(v)
    k, sep, val = v.partition("=")
    if sep and k and not any(ch in k for ch in " /\\:"):
        return k + "=" + redact_value(k, val)
    k, sep, val = v.partition(":")
    if sep and k and not any(ch in k for ch in " /\\") and _auth_header(k):
        # A quote left after the colon is the end of a quoted name (`'X-Api-Key:'`),
        # not its value: the value is the next word, which the split-header pass takes.
        rest = val.strip().strip("\"'")
        if rest.lower().startswith(("bearer ", "basic ")):
            rest = rest[rest.index(" ") + 1:].strip()
        if rest == "" or _pure_reference(rest):
            return v
        return k + ": " + REDACTED
    return v


# curl's -u/--user name:password: the part after the first ':' is the password.
_USER_FLAGS = ("u", "user")


def _redact_user(v: str) -> str:
    """The password replaced; the rest still gets the positional checks, since an
    API key is often passed as the user name with an empty password (`-u key:`)."""
    user, sep, pw = v.partition(":")
    if sep and pw and not _pure_reference(pw):
        return redact_value("", user) + ":" + REDACTED
    return redact_value("", v)


def redact_args(args: list[str]) -> list[str]:
    out = list(args)
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            flag, sep, val = a.lstrip("-").partition("=")
            if sep:
                red = _redact_user(val) if flag in _USER_FLAGS else redact_value(flag, val)
                out[i] = a[:len(a) - len(val)] + red
            elif _secret_name(flag) and i + 1 < len(args) and not args[i + 1].startswith("-"):
                out[i + 1] = redact_value(flag, args[i + 1])
                i += 1
            elif flag in _USER_FLAGS and i + 1 < len(args) and not args[i + 1].startswith("-"):
                out[i + 1] = _redact_user(args[i + 1])
                i += 1
        else:
            out[i] = redact_value("", a)
        i += 1
    return out


def _bare(word: str) -> str:
    return word.strip("\"'")


def _redact_split_headers(words: list[str]) -> list[str]:
    """What splitting on whitespace breaks apart. A command line read back as
    one string has lost its quoting, so `-H "Authorization: Bearer abc"`
    arrives as four words and the word-by-word pass sees `Bearer` and `abc`
    apart: the secret after a bare `Bearer` or `Basic`, and the value after a
    secret header name whose own word ends at its colon, are replaced here."""
    out = list(words)
    i = 0
    while i < len(out) - 1:
        w = _bare(out[i])
        nxt = _bare(out[i + 1])
        if w.lower() in ("bearer", "basic") and nxt and nxt != REDACTED and not _pure_reference(nxt):
            out[i + 1] = REDACTED
            i += 2
            continue
        k, sep, val = w.partition(":")
        if (sep and not val and k and not any(ch in k for ch in " /\\") and _auth_header(k)
                and nxt and nxt.lower() not in ("bearer", "basic") and nxt != REDACTED
                and not _pure_reference(nxt)):
            out[i + 1] = REDACTED
            i += 2
            continue
        i += 1
    return out


def redact_command(cmd) -> str:
    """A command line with its secrets replaced, split and joined on
    whitespace as the bastion's console does (strings.Fields), and with the
    header values that splitting tore off their names replaced too."""
    if not isinstance(cmd, str) or not cmd:
        return ""
    return " ".join(_redact_split_headers(redact_args(cmd.split())))
