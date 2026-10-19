"""What a Host is called on screen when it has no real name yet.

The word "entity" is never shown; it is a Host. Existing pods can
still hold a placeholder display name in entity_registry: the init-db seed
("Entity", now "Host" for new rows), the genesis default ("Entity N", now
"Host #N"), or a provisioning-era "entity-N". Those rows are not migrated.
Every place that SHOWS the name to a person, or hands it to the Host, passes it
through display_name_or_host() instead, so a placeholder reads "Host #N".

The JS twin for the Settings app lives in system-apps/settings/app.js
(isPlaceholderName). Keep the two pattern sets identical;
scripts/test_host_display_names.py checks both against the same cases.
"""
from __future__ import annotations

import re

# EXACTLY the names the platform generates — matched on the whole trimmed
# name, any case — and nothing a user could have chosen on purpose ("Host 7"
# without the #, "Entity #7", "Host-7" are all real names and are shown):
#   "Entity" / "Host"      the init-db seed row (old / new)
#   "Entity 7"             the old genesis default (f"Entity {id}")
#   "Host #7"              the new genesis default (f"Host #{id}")
#   "entity-7"             a provisioning-era name (the gitea login)
#   "—"                    system.py's no-id marker
PLACEHOLDER_RE = re.compile(r"^(?:entity|host|entity \d+|host #\d+|entity-\d+|—)$", re.I)


def is_placeholder_name(name: object) -> bool:
    """True when `name` is empty or one of the generated placeholders above,
    i.e. not a name anybody chose."""
    s = str(name or "").strip()
    return not s or bool(PLACEHOLDER_RE.match(s))


def host_label(host_id: object) -> str:
    """"Host #N" for a real id, else "Host"."""
    try:
        n = int(host_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "Host"
    return f"Host #{n}" if n > 0 else "Host"


def display_name_or_host(name: object, host_id: object = None) -> str:
    """The name to show: the stored name when someone chose it, otherwise
    "Host #N" (or "Host" when the id is unknown)."""
    if is_placeholder_name(name):
        return host_label(host_id)
    return str(name).strip()


def host_self_intro(name: object, host_id: object = None) -> str:
    """The first line of the Host's identity prompt. A chosen name reads
    "You are Ada, the user's Host."; a placeholder reads "You are the
    user's Host (Host #3)." rather than "You are Host #3, the user's Host."."""
    if is_placeholder_name(name):
        label = host_label(host_id)
        return "You are the user's Host." if label == "Host" else f"You are the user's Host ({label})."
    return f"You are {str(name).strip()}, the user's Host."
