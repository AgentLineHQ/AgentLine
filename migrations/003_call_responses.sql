-- Migration: Create call_responses table for API-driven voice responses
-- This table queues text responses that agents send to be spoken on active calls.

CREATE TABLE IF NOT EXISTS call_responses (
    id SERIAL PRIMARY KEY,
    call_id TEXT REFERENCES calls(id) ON DELETE CASCADE,
    response_text TEXT NOT NULL,
    spoken BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_call_responses_pending
    ON call_responses (call_id, spoken)
    WHERE spoken = false;
