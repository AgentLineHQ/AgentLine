-- Migration 007: Monthly $2 rental for active phone numbers
-- Tracks when each number was last billed so recurring fees can be applied
-- one month after provision (and every month thereafter while active).

ALTER TABLE phone_numbers
    ADD COLUMN IF NOT EXISTS last_billed_at TIMESTAMPTZ;

-- Existing numbers: treat provision time as last bill so the next charge
-- falls one calendar month after they were first provisioned.
UPDATE phone_numbers
   SET last_billed_at = created_at
 WHERE last_billed_at IS NULL
   AND created_at IS NOT NULL;
