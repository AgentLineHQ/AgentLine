-- AgentLine Database Schema
-- Run via Alembic migration or directly against PostgreSQL

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE accounts (
    id               TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    human_email      TEXT UNIQUE NOT NULL,
    supabase_user_id TEXT UNIQUE,   -- Links to Supabase Auth user
    balance          NUMERIC(12,4) NOT NULL DEFAULT 10.0000,  -- USD balance, starts with $10
    default_voice_id TEXT,          -- Account-level default Cartesia voice UUID
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE api_keys (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id  TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    key_hash    TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);

CREATE TABLE agents (
    id               TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id       TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    voice_mode       TEXT DEFAULT 'hosted',
    system_prompt    TEXT,
    initial_greeting TEXT,
    voice_id         TEXT,          -- Cartesia UUID, preset name, or NULL (resolves to system default)
    model_tier       TEXT DEFAULT 'balanced',
    transfer_number  TEXT,
    voicemail_message TEXT,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE phone_numbers (
    id             TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id     TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    agent_id       TEXT REFERENCES agents(id),
    provider_id    TEXT UNIQUE NOT NULL,  -- Plivo uses raw number as ID
    phone_number   TEXT UNIQUE NOT NULL,
    country        TEXT DEFAULT 'IN',
    status         TEXT DEFAULT 'active',
    created_at     TIMESTAMPTZ DEFAULT now(),
    released_at    TIMESTAMPTZ
);

-- Enforce: each agent can only have ONE active number
CREATE UNIQUE INDEX idx_one_active_number_per_agent
    ON phone_numbers (agent_id)
    WHERE status = 'active';

CREATE TABLE calls (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id        TEXT REFERENCES accounts(id),
    agent_id          TEXT REFERENCES agents(id),
    number_id         TEXT REFERENCES phone_numbers(id),
    provider_call_id  TEXT,  -- Was telnyx_call_id, now generic
    direction         TEXT NOT NULL,
    from_number       TEXT NOT NULL,
    to_number         TEXT NOT NULL,
    status            TEXT DEFAULT 'initiated',
    system_prompt     TEXT,
    voice_id          TEXT,  -- Per-call voice override (Cartesia UUID)
    duration_seconds  INTEGER,
    transcript        JSONB DEFAULT '[]',
    started_at        TIMESTAMPTZ DEFAULT now(),
    ended_at          TIMESTAMPTZ
);

CREATE TABLE messages (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id          TEXT REFERENCES accounts(id),
    agent_id            TEXT REFERENCES agents(id),
    number_id           TEXT REFERENCES phone_numbers(id),
    conversation_id     TEXT,
    provider_message_id TEXT,  -- Was telnyx_message_id, now generic
    direction           TEXT NOT NULL,
    from_number         TEXT NOT NULL,
    to_number           TEXT NOT NULL,
    body                TEXT,
    media_url           TEXT,
    status              TEXT DEFAULT 'sent',
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE conversations (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id      TEXT REFERENCES accounts(id),
    agent_id        TEXT REFERENCES agents(id),
    number_id       TEXT REFERENCES phone_numbers(id),
    contact_number  TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_message_at TIMESTAMPTZ,
    UNIQUE(number_id, contact_number)
);

CREATE TABLE webhooks (
    id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    agent_id   TEXT REFERENCES agents(id),
    url        TEXT NOT NULL,
    secret     TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Agent response queue: text that agents POST via /v1/calls/{id}/speak
-- gets spoken on the active call by the Plivo wait loop.
CREATE TABLE IF NOT EXISTS call_responses (
    id SERIAL PRIMARY KEY,
    call_id TEXT REFERENCES calls(id) ON DELETE CASCADE,
    response_text TEXT NOT NULL,
    spoken BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now()
);


-- Event Mailbox: server-side event queue for agents that can't expose
-- a public webhook URL. Events are stored temporarily and pulled
-- by agents via GET /v1/events.
CREATE TABLE IF NOT EXISTS event_mailbox (
    id          SERIAL PRIMARY KEY,
    event_id    TEXT UNIQUE NOT NULL,
    account_id  TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    agent_id    TEXT REFERENCES agents(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Billing ledger: every debit/credit is an immutable row
CREATE TABLE IF NOT EXISTS billing_ledger (
    id              SERIAL PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amount          NUMERIC(12,4) NOT NULL,   -- negative = debit, positive = credit
    balance_after   NUMERIC(12,4) NOT NULL,   -- snapshot of balance after this txn
    txn_type        TEXT NOT NULL,             -- 'call_charge', 'number_provision', 'topup', 'refund'
    reference_id    TEXT,                      -- call_id, number_id, or payment_id
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);


-- Performance indexes
CREATE INDEX idx_api_keys_prefix ON api_keys(key_prefix) WHERE revoked_at IS NULL;
CREATE INDEX idx_agents_account ON agents(account_id);
CREATE INDEX idx_numbers_agent ON phone_numbers(agent_id) WHERE status = 'active';
CREATE INDEX idx_numbers_phone ON phone_numbers(phone_number);
CREATE INDEX idx_calls_account ON calls(account_id, started_at DESC);
CREATE INDEX idx_calls_agent ON calls(agent_id, started_at DESC);
CREATE INDEX idx_messages_account ON messages(account_id, created_at DESC);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at DESC);
CREATE INDEX idx_conversations_number_contact ON conversations(number_id, contact_number);
CREATE INDEX idx_webhooks_agent ON webhooks(agent_id);
CREATE INDEX idx_webhooks_account ON webhooks(account_id) WHERE agent_id IS NULL;
CREATE INDEX idx_accounts_supabase ON accounts(supabase_user_id) WHERE supabase_user_id IS NOT NULL;
CREATE INDEX idx_event_mailbox_account ON event_mailbox(account_id, created_at ASC);
CREATE INDEX idx_event_mailbox_agent ON event_mailbox(account_id, agent_id, created_at ASC);
CREATE INDEX idx_event_mailbox_expiry ON event_mailbox(account_id, created_at);
CREATE INDEX idx_billing_ledger_account ON billing_ledger(account_id, created_at DESC);
CREATE INDEX idx_billing_ledger_type ON billing_ledger(account_id, txn_type);
