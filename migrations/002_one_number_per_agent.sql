-- Migration: Enforce one active number per agent
-- Run this on your Supabase database

-- Create a partial unique index: only one active number per agent
-- This allows released numbers to remain in history without conflict
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_number_per_agent
    ON phone_numbers (agent_id)
    WHERE status = 'active';

-- Ensure status column has a default value
ALTER TABLE phone_numbers ALTER COLUMN status SET DEFAULT 'active';
