-- Add owner_phone column for owner call detection / task mode
-- Calls from this number trigger task mode: the agent treats speech as executable instructions.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_phone TEXT;
