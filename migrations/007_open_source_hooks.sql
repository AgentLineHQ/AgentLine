-- Migration 007: open-source provider hooks
-- Drops hosted billing and the Supabase auth link.
-- Adds the columns the provider and voice-runtime hooks read.

DROP INDEX IF EXISTS idx_billing_ledger_account;
DROP INDEX IF EXISTS idx_billing_ledger_type;
DROP TABLE IF EXISTS billing_ledger;

ALTER TABLE accounts DROP COLUMN IF EXISTS balance;
ALTER TABLE accounts DROP COLUMN IF EXISTS supabase_user_id;
DROP INDEX IF EXISTS idx_accounts_supabase;

ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS voice_runtime TEXT;
