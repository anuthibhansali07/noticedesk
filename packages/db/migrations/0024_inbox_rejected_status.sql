-- 0024_inbox_rejected_status.sql
-- Adds 'rejected' as a documents_inbox.routing_status value.
--
-- Bug (Nov 2026): POST /v1/inbox/{id}/reject wrote routing_status =
-- 'manual_assignment' — the status a row needing manual assignment already
-- has — and kept the rejection only inside routing_anomaly_details. The
-- inbox row therefore looked unchanged after "Reject" (same chip, same
-- Assign manually / Reject buttons). The Java backend already added
-- 'rejected' in its Flyway V19; this brings the Python schema in line.

ALTER TABLE documents_inbox
    DROP CONSTRAINT IF EXISTS documents_inbox_routing_status_check;
ALTER TABLE documents_inbox
    ADD CONSTRAINT documents_inbox_routing_status_check
    CHECK (routing_status IN (
        'pending', 'routed', 'client_not_found',
        'new_gst_registration_detected', 'pan_gstin_mismatch',
        'no_identifier_found', 'manual_assignment', 'rejected'
    ));

-- Backfill: rows rejected under the old behaviour carry
-- {"rejected": true, ...} in routing_anomaly_details.
UPDATE documents_inbox
SET routing_status = 'rejected'
WHERE routing_status <> 'routed'
  AND (routing_anomaly_details ->> 'rejected')::BOOLEAN IS TRUE;
