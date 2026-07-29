-- callmem schema v16: Retry backoff and requeue tracking for jobs
--
-- next_attempt_at: nullable timestamp. fail() sets it to an exponential
-- backoff (60s, 240s, 960s for attempts 1, 2, 3) when requeuing a job for
-- retry; dequeue() skips jobs whose next_attempt_at is still in the
-- future. Existing rows are NULL and dequeue immediately, as before.
--
-- requeue_count: how many times a permanently-failed job has been
-- auto-resurrected by the worker after a same-type job completed
-- successfully. Caps runaway retry loops (manual `requeue-failed` ignores
-- this cap).

ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT;
ALTER TABLE jobs ADD COLUMN requeue_count INTEGER NOT NULL DEFAULT 0;
