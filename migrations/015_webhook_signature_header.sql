-- Configurable HMAC signature header name for per-agent webhooks.
-- Lets consumers match the header their platform natively verifies
-- (X-Webhook-Signature, X-Hub-Signature-256, etc.).
ALTER TABLE webhooks
    ADD COLUMN IF NOT EXISTS signature_header TEXT DEFAULT 'X-Webhook-Signature';
