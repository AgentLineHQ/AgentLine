-- Migration 008: Track last low-balance email send time (Resend rate limit)
-- Used to avoid spamming the account owner when monthly number fees
-- repeatedly fail due to insufficient balance.

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS last_low_balance_email_at TIMESTAMPTZ;
