-- v126_machine_map.sql — the machine map the host plans from
--
-- The host should plan from a MAP of the user's machine, not from a
-- screenshot. The bastion answers two read-only management ops
-- (computer:inventory — installed apps with version, publisher, path and
-- ways_in, deepest first; computer:windows — the open windows with owner and
-- focus). This table keeps the latest report per (entity, server, kind) so
-- the host can consult the map on many turns for one round trip to the box.
--
-- The report is stored as the bastion sent it, `supported`/`reason` included:
-- a box that cannot look (no display, no wmctrl, macOS Accessibility not
-- granted) is kept as blind, never as empty. Refreshing runs through the
-- signed management grant like every op (nac_mgmt.run_mgmt_op); a denied or
-- unreachable refresh leaves the previous report in place.
--
-- The runtime also creates this table defensively (server/network/machine_map.py
-- ensure_table); this file is the authoritative record.

CREATE TABLE IF NOT EXISTS network_machine_map (
    entity_id  INTEGER NOT NULL,
    server     TEXT NOT NULL,
    kind       TEXT NOT NULL,           -- 'apps' | 'windows'
    report     JSONB NOT NULL,          -- the bastion's report, verbatim
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, server, kind)
);
