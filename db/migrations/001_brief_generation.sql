-- Stop API and LLM workers before applying, then restart both with the new code.
-- Existing queued jobs may claim a NULL generation; regenerate supersedes them.
ALTER TABLE case_briefs ADD COLUMN IF NOT EXISTS generation_id TEXT;
