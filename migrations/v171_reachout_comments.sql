-- v171 — reach-out's column comments say what is true now.
--
-- v152 said the Oversight events were "declared and not yet emitted by
-- anything" — the oversight ledger emits both (brain/mastercode/runtime/
-- oversight.py, EVENT_CRITICAL / EVENT_HIGH) — and listed `fanout_finished`,
-- which session.py writes as `compare_finished`. It also said the host may
-- delete what it created without saying that is ALL it may delete: the Gmail
-- channel the platform sets up at connect is the user's to remove, not the
-- host's (reachout/service.remove_channel). A column comment is what `\d+`
-- shows the next reader, so it is corrected in the database, not only in the
-- file that first wrote it.
--
-- Inside a to_regclass guard, COMMENTs included, for the reason v146 records:
-- a bare statement after the guard would abort startup (ON_ERROR_STOP) on a
-- host whose v152 never applied — exactly the case the dispatcher's
-- schema-missing notice exists for.

DO $$
BEGIN
    IF to_regclass('public.reachout_triggers') IS NULL
       OR to_regclass('public.reachout_channels') IS NULL THEN
        RAISE NOTICE 'v171: reach-out tables absent — nothing to comment';
        RETURN;
    END IF;

    COMMENT ON COLUMN reachout_triggers.match_event IS
        'payload.event. Emitted today: build_finished, compare_finished, '
        'app_proven, agent_finished (runtime/session.py), site_published '
        '(routes/applications.py), and oversight_critical / oversight_high from '
        'the oversight ledger (runtime/oversight.py). A trigger on any other '
        'name is accepted and fires once something emits it. A trigger never '
        'matches an inbox row older than itself (reachout/dispatcher.py).';

    COMMENT ON COLUMN reachout_channels.created_by IS
        'host | user | platform. The host may remove only what IT created. A '
        'row the user created, and the Gmail channel the platform sets up when '
        'Gmail is connected, are the user''s to remove, from Circles.';
END $$;
