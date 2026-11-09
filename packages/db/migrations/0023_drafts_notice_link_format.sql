-- 0023_drafts_notice_link_format.sql
-- Links each draft to the notice it answers and records the reply format.
--
-- Bug (Nov 2026 drafting audit, finding 5.1): the DOCX cover sheet picked
-- "the open notice with the earliest due date" on the draft's matter. On a
-- matter carrying several notices (e.g. a s.142(1) inquiry and a s.143(2)
-- scrutiny notice) the s.143(2) reply was exported with the s.142(1)
-- document type and due date. Storing notice_id on the draft lets the
-- export read the right notice.

ALTER TABLE drafts
    ADD COLUMN IF NOT EXISTS notice_id      UUID REFERENCES notices (notice_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS format_profile TEXT;

ALTER TABLE drafts
    DROP CONSTRAINT IF EXISTS drafts_format_profile_check;
ALTER TABLE drafts
    ADD CONSTRAINT drafts_format_profile_check
    CHECK (
        format_profile IS NULL
        OR format_profile IN ('full_submission', 'tabular_reconciliation', 'adjournment')
    );

CREATE INDEX IF NOT EXISTS idx_drafts_notice ON drafts (notice_id);

-- Backfill: unambiguous only — matters with exactly one notice.
UPDATE drafts d
SET notice_id = n.notice_id
FROM (
    SELECT matter_id, MIN(notice_id::TEXT)::UUID AS notice_id
    FROM notices
    GROUP BY matter_id
    HAVING COUNT(*) = 1
) n
WHERE d.notice_id IS NULL AND d.matter_id = n.matter_id;

UPDATE drafts SET format_profile = 'full_submission' WHERE format_profile IS NULL;
