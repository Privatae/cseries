"""
Integration framework — four-object data model.

This is the producer half of C-Series's integration platform. Every
provider (Gmail, Plaid, Notion, ...) is a Provider manifest + optional
provider-specific code. A user creates Connections against providers;
each connection owns cursor-based Syncs per resource; every API call
or data change emits Signals onto the already-existing signal bus.

Paradigm: deterministic, manifest-driven, stateless-by-default. State
lives in two places: (1) the Host's Postgres for connections + syncs +
canonical data (integration_* tables, v201), (2) the credentials vault for
tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class AuthMode(str, Enum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    BASIC = "basic"
    HOSTED_LINK = "hosted_link"  # Plaid Link widget pattern
    NONE = "none"


class ConnectionStatus(str, Enum):
    INITIATED = "initiated"     # OAuth flow started, no tokens yet
    ACTIVE = "active"           # healthy, syncing
    NEEDS_REAUTH = "needs_reauth"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ERROR = "error"


class SyncStrategy(str, Enum):
    CURSOR = "cursor"                # stateful, incremental (Gmail historyId, Plaid next_cursor)
    INCREMENTAL_POLL = "incremental_poll"  # updated-since-timestamp polling
    FULL_POLL = "full_poll"          # no cursor support, full re-read
    WEBHOOK = "webhook"              # provider pushes; we react
    FILE_WATCH = "file_watch"        # inotify on a local mount


@dataclass
class RateLimit:
    requests_per_minute: int = 60
    concurrent: int = 4


@dataclass
class ResourceSpec:
    """A canonical resource a provider exposes (email, transaction, event)."""
    name: str                                 # "email", "transaction"
    canonical_model: Optional[str] = None     # "Email", "Transaction", or None for raw-only
    sync: dict[str, Any] = field(default_factory=dict)
    signal: dict[str, Any] = field(default_factory=dict)


@dataclass
class Provider:
    """Declarative provider definition loaded from YAML manifest."""
    key: str
    display_name: str
    categories: list[str] = field(default_factory=list)
    connection_kind: str = ""        # feeds portal_access.metadata.connection_kind
    connection_provider: str = ""    # feeds portal_access.metadata.connection_provider
    auth: dict[str, Any] = field(default_factory=dict)
    proxy: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    resources: list[ResourceSpec] = field(default_factory=list)
    webhooks: dict[str, Any] = field(default_factory=dict)
    post_connection: list[Any] = field(default_factory=list)
    reauth_triggers: list[str] = field(default_factory=list)
    rate_limit: Optional[RateLimit] = None
    # realtime_voice providers only: {ws_url_template, audio_format, token_url}.
    # token_url is the ephemeral-token mint endpoint the KERNEL calls with the
    # user's sealed key (routes/realtime.py); ws_url_template is what the browser
    # app opens afterwards. Both are egress-checked at proposal time.
    realtime: dict[str, Any] = field(default_factory=dict)

    @property
    def auth_mode(self) -> AuthMode:
        mode = (self.auth or {}).get("mode", "oauth2")
        try:
            return AuthMode(mode)
        except ValueError:
            return AuthMode.NONE

    def resource(self, name: str) -> Optional[ResourceSpec]:
        for r in self.resources:
            if r.name == name:
                return r
        return None


@dataclass
class Connection:
    """A user's live binding to a Provider. Analogous to Plaid's Item +
    Composio's connected_account."""
    id: str
    provider_key: str
    user_id: str                               # entity_id as string
    status: ConnectionStatus
    credentials_ref: Optional[str] = None      # opaque pointer into credentials vault
    connection_config: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None


@dataclass
class Sync:
    """Per-resource cursor state."""
    connection_id: str
    resource: str
    cursor: Optional[str] = None
    mode: SyncStrategy = SyncStrategy.CURSOR
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_error: Optional[str] = None
