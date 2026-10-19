-- v095: filename words become searchable lexemes
--
-- The FTS parser tokenizes 'budget.csv' as ONE file token, so a search
-- for "budget" could never match it lexically — while 'spend account.csv'
-- matched "spend" only because of the space. The write path (_TSV_SQL) now
-- indexes the title verbatim AND separator-normalized; this backfills every
-- existing row the same way.
--
-- pg-resident rows rebuild from title + normalized title + content.
-- External/base64 rows (content is a marker or base64 blob) rebuild from
-- the title pair only — raw bytes must never enter search_tsv.

BEGIN;

UPDATE user_context
SET search_tsv = to_tsvector('english', left(
        coalesce(title, '') || ' ' ||
        regexp_replace(coalesce(title, ''), '[._\-/]+', ' ', 'g') || ' ' ||
        content, 200000))
WHERE COALESCE(storage_backend, 'pg') = 'pg'
  AND COALESCE(metadata->>'encoding', '') != 'base64';

UPDATE user_context
SET search_tsv = to_tsvector('english',
        coalesce(title, '') || ' ' ||
        regexp_replace(coalesce(title, ''), '[._\-/]+', ' ', 'g'))
WHERE COALESCE(storage_backend, 'pg') != 'pg'
   OR COALESCE(metadata->>'encoding', '') = 'base64';

COMMIT;
