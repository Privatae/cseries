-- v197 (2026-10-01): the phase status 'review' (landed, not proven) is retired.
--
-- A build either landed its files or the harness failed.
-- Nothing writes 'review' any more (brain/mastercode/runtime/tools.py
-- _run_phase_build); a phase that landed in it before today did land, so it is
-- 'complete'. A project whose phases have now all landed or been retired is
-- complete, the same rule the build path applies when its last phase lands.
-- Nothing is merged or shipped here: that stays the person's merge, or their
-- Auto-Approve on the next build.
--
-- The CTE's update is not visible to the outer statement's subquery (one
-- snapshot), so 'review' counts as landed there too.
WITH landed AS (
    UPDATE project_phases SET status = 'complete'
     WHERE status = 'review'
    RETURNING project_id
)
UPDATE projects p
   SET status = 'complete', updated_at = now()
 WHERE p.id IN (SELECT project_id FROM landed)
   -- An archived project stays archived: un-archiving it would put it back in
   -- front of the person, and could collide with a live project that has
   -- since mounted its repo (v191's uq_projects_live_repo) and stop the boot.
   AND p.status NOT IN ('complete', 'archived')
   AND NOT EXISTS (
        SELECT 1 FROM project_phases ph
         WHERE ph.project_id = p.id
           AND ph.status NOT IN ('complete', 'archived', 'review'));
