-- Migration 006: Voice Settings
-- Adds account-level default voice and per-call voice override support.
--
-- Voice resolution chain (highest priority wins):
--   1. Per-call voice_id  (calls.voice_id)
--   2. Agent voice_id     (agents.voice_id — already exists)
--   3. Account default    (accounts.default_voice_id — NEW)
--   4. System default     (hardcoded in voice/voices.py)
--
-- SAFETY: All new columns are nullable with no NOT NULL constraints.
--         IF NOT EXISTS guards make this migration idempotent (safe to re-run).
--         Existing accounts, agents, and calls are NOT modified.

-- Account-level default voice: applies to ALL agents under this account
-- unless overridden at the agent or per-call level.
ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS default_voice_id TEXT;

-- Per-call voice override: allows a specific call to use a different voice
-- than what the agent or account is configured for.
ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS voice_id TEXT;

-- Fix the agents.voice_id column default from the old string
-- 'cartesia-sonic-english' to NULL (let the voice catalog resolve it).
-- NOTE: This only changes the DEFAULT for *future* rows. Existing agents
--       keep their current voice_id values untouched.
ALTER TABLE agents
    ALTER COLUMN voice_id SET DEFAULT NULL;
