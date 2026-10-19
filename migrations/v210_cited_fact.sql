-- v210: memory kind five, the cited fact.
--
-- A cited fact is a finding from a task's record (v208 task_findings) that
-- the Host promoted into memory with task_record op='promote': the claim as
-- its content, and in its metadata the source's exact words, the source and
-- the version they were checked against, when it was read and checked, and
-- the project. It is recalled as "source said 'quote' (read date)", fenced as
-- data, never as the Host's own belief; re-checked by code when the same
-- source is read at a new version (runtime/task_record.recheck_source).
--
-- The CHECK list as v103 left it, plus 'cited_fact'.

BEGIN;

ALTER TABLE entity_memory
    DROP CONSTRAINT IF EXISTS entity_memory_memory_type_check;

ALTER TABLE entity_memory
    ADD CONSTRAINT entity_memory_memory_type_check
    CHECK (memory_type IN (
        'fact','preference','goal','emotion','topic',
        'relationship','decision','unresolved','summary',
        'skill_memory',
        'moral_precedent','constitutional','legacy','knowledge',
        'turn','research','governance_observation',
        'cited_fact'
    ));

COMMIT;
