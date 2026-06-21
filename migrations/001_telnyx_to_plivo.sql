-- Migration: Rename Telnyx-specific columns to generic provider columns
-- Run this on your existing database BEFORE deploying the Plivo update
-- This is a non-destructive rename — no data is lost.

-- 1. Rename telnyx_id → provider_id in phone_numbers
ALTER TABLE phone_numbers RENAME COLUMN telnyx_id TO provider_id;

-- 2. Rename telnyx_call_id → provider_call_id in calls
ALTER TABLE calls RENAME COLUMN telnyx_call_id TO provider_call_id;

-- 3. Rename telnyx_message_id → provider_message_id in messages
ALTER TABLE messages RENAME COLUMN telnyx_message_id TO provider_message_id;
