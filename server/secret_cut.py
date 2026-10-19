"""The person's words with their secret shapes cut, for every door that reads
them before the turn does.

The turn cuts a message before anything of it sees it: runtime/session.py's
send_message runs mastercode/secret_shapes.redact on the message, which
replaces every hard shape (a provider key, a token, a private key, a password
in a link) with its [REDACTED:<id>] marker and leaves soft ones (an assignment,
a random-looking token) as they are; a screen that fails is logged and the
turn goes on with the words as they were. Some doors read the words earlier,
and each of those calls out of the Host: a search embeds what is typed on the
Host's embedding endpoint, which can be a keyed outside provider; a pick sends
a message to the Host's chat model before its turn starts; an outside client
hands the Host a turn it ran itself, which the Host embeds and keeps. Each
door cuts the words here first, by the same rules and to the same result.

  redact_words   the words as the turn reads them: every hard shape replaced
                 by its marker. For words a model reads or a row keeps.
  search_words   the same with the markers left out too: for a query, where a
                 marker's own words ("REDACTED", the rule's id) would find
                 pages of their own.

`door` names the caller in the log line; `log` is the caller's logger, so a
cut is logged where that door logs everything else.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

_log = logging.getLogger("privatae.pc.secret_cut")

# The marker secret_shapes.redact writes (secret_shapes.json redaction_format).
REDACTED_RE = re.compile(r"\[REDACTED:[^\]]*\]")


def redact_words(text, *, door: str, log: Optional[logging.Logger] = None) -> str:
    """`text` with every hard secret shape replaced by its [REDACTED:<id>]
    marker: the rules file and the call runtime/session.send_message applies
    to a message before anything of the turn sees it. A redactor that cannot
    load leaves the words as they are and says so in the log, as
    session.send_message does."""
    log = log or _log
    text = str(text or "")
    try:
        from mastercode import secret_shapes
        cleaned, findings = secret_shapes.redact(text)
        hard = secret_shapes.hard(findings)
    except Exception as e:  # noqa: BLE001 — as session.send_message: logged, the words kept
        log.warning("%s: secret-shape screen failed (non-fatal): %s", door, e)
        return text
    if hard:
        log.warning("%s: secret shapes cut from the person's words: %s", door, [f.id for f in hard])
    return cleaned


def search_words(text, *, door: str, log: Optional[logging.Logger] = None) -> str:
    """What a search reads of `text`: its words with the secrets cut, and the
    cut markers left out too (their words would match pages of their own)."""
    return REDACTED_RE.sub(" ", redact_words(text, door=door, log=log))
