-- callmem schema v11: Output quality scoring (A3)
--
-- Adds optional eval_score, eval_feedback, and eval_model columns to
-- events and entities so a judge model (or self-assessment) can record
-- quality signals alongside the raw data.

ALTER TABLE events ADD COLUMN eval_score REAL;
ALTER TABLE events ADD COLUMN eval_feedback TEXT;
ALTER TABLE events ADD COLUMN eval_model TEXT;

ALTER TABLE entities ADD COLUMN eval_score REAL;
ALTER TABLE entities ADD COLUMN eval_feedback TEXT;
