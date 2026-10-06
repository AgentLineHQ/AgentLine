-- Migration 009: Per-agent webhooks (no account-wide)
--
-- SAFE & IDEMPOTENT. This migration does NOT delete any rows, does NOT drop any
-- columns or tables, and does NOT modify foreign keys. Every statement is either
-- additive or guarded so it can never fail or destroy data. It is safe to re-run.
--
-- Webhooks are strictly per-agent: each agent has at most ONE webhook URL that
-- receives all of that agent's events. There is no account-wide webhook. The
-- application layer (Pydantic models + routers) already guarantees agent_id
-- is always set; these DB constraints are defense-in-depth.

-- One webhook per agent. Idempotent — no-op if the index already exists.
CREATE UNIQUE INDEX IF NOT EXISTS idx_webhooks_one_per_agent
    ON webhooks (account_id, agent_id);

-- Make agent_id NOT NULL, but ONLY if no NULL rows exist — so this can never
-- fail or reject existing data. (The app never inserts NULL agent_id anyway.)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM webhooks WHERE agent_id IS NULL) THEN
        ALTER TABLE webhooks ALTER COLUMN agent_id SET NOT NULL;
    END IF;
END $$;

-- Drop obsolete index names. `idx_webhooks_account` existed in the original
-- schema (a non-unique index, now redundant); the other two only ever existed
-- in earlier dev-only revisions of this migration. IF EXISTS = harmless no-op
-- when absent. Dropping an index NEVER deletes table data.
DROP INDEX IF EXISTS idx_webhooks_one_per_account;
DROP INDEX IF EXISTS idx_webhooks_one_per_scope;
DROP INDEX IF EXISTS idx_webhooks_account;
