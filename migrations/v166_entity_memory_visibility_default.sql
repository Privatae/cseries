-- v166 — rename the stray entity_memory.visibility default: 'topic' -> 'default'.
--
-- WHY. init-db.sql:153 declares `visibility TEXT NOT NULL DEFAULT 'topic'`.
-- docs/MEMORY_ARCHITECTURE.md:30 calls the whole column "STUB, barely used" —
-- but a code search (2026-09-22)
-- shows the DEFAULT itself is not what most rows carry: `classify_visibility`
-- (memory_engine.py:1669-1675) actively returns the string 'topic' for any
-- memory that doesn't match a CONTEXT_SIGNALS phrase, and is called on every
-- consolidated memory at write time (memory_engine.py:2431, 2483;
-- batch_consolidator.py:863). So 'topic' is a live classification value, not
-- just an unused column default.
--
-- What was checked and found ABSENT: no reader anywhere in cseries filters on
-- the literal value 'topic' (a code search over `visibility` — 80 matches — and
-- targeted regex `visibility\s*(!=|<>|IN|=)` — 20 matches — show `visibility`
-- used only as (a) a query PARAMETER name threaded through retrieve() /
-- retrieve_by_keywords() / memory_consolidation.py, whose value comes from a
-- caller-supplied `consumer`/mode, and (b) the `directive_visibility` /
-- browser-session / harness-spec `visibility` fields, which are unrelated
-- columns/attributes entirely. No call site was found passing the literal
-- string 'topic' or 'context' as that parameter. So no code path depends on
-- an EXISTING row's stored value being exactly 'topic'.
--
-- This migration is therefore safe to backfill existing rows. As of this
-- branch, every writer has also been updated to emit the new value:
-- `classify_visibility` (brain/mastercode/memory_engine.py:1706-1718) now
-- returns 'default' (or 'context'), and the hardcoded literals/defaults in
-- memory_consolidation.py and runtime/skill_events.py were changed from
-- 'topic' to 'default' in the same pass. So after this migration + those
-- writer changes, every entity_memory row — existing (backfilled here) and
-- future (written as 'default'/'context') — uses the new vocabulary; no
-- writer still emits 'topic'. The rename is inert for behaviour: no reader
-- anywhere filters on the literal value (see the ABSENT check above), so
-- this is a pure relabeling, not a semantic change.
ALTER TABLE entity_memory ALTER COLUMN visibility SET DEFAULT 'default';

UPDATE entity_memory
   SET visibility = 'default'
 WHERE visibility = 'topic';
