-- v228: the owner's login for a standalone Host (server/owner_login.py).
--
-- One owner per Host. The password is stored only as a scrypt hash, and a
-- session only as the SHA-256 of its cookie value, so a copy of this
-- database cannot be used to sign in.

BEGIN;

CREATE TABLE IF NOT EXISTS owner_account (
    id            SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    -- Part of the session cookie's name, so two Hosts on one machine (which
    -- share every cookie of that host name) do not sign each other out.
    cookie_suffix TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS owner_sessions (
    token_hash   TEXT PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    -- The host name the session was issued on; it is refused on any other.
    origin       TEXT NOT NULL,
    -- An app origin's session hangs off the desktop session it joined from:
    -- it lives only while that one does, and goes when it is signed out.
    parent       TEXT REFERENCES owner_sessions (token_hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS owner_sessions_expires ON owner_sessions (expires_at);
CREATE INDEX IF NOT EXISTS owner_sessions_parent ON owner_sessions (parent);

COMMIT;

-- The AI's read lane (host_recall, v139) is handed SELECT on every new table
-- by default privileges. These two hold the password hash and the session
-- hashes, so they are taken back here, and v139 (which runs on every boot)
-- lists them with its other exceptions.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_recall') THEN
        REVOKE ALL ON public.owner_account FROM host_recall CASCADE;
        REVOKE ALL ON public.owner_sessions FROM host_recall CASCADE;
    END IF;
END $$;
