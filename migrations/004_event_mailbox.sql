-- Migration 004: Event Mailbox
-- Server-side event queue for agents that can't expose a public webhook URL.
-- Events are stored temporarily and pulled by agents via GET /v1/events.

CREATE TABLE IF NOT EXISTS event_mailbox (
    id          SERIAL PRIMARY KEY,
    event_id    TEXT UNIQUE NOT NULL,
    account_id  TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    agent_id    TEXT REFERENCES agents(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Fast lookups by account + agent
CREATE INDEX idx_event_mailbox_account ON event_mailbox(account_id, created_at ASC);
CREATE INDEX idx_event_mailbox_agent ON event_mailbox(account_id, agent_id, created_at ASC);

-- Auto-cleanup: events expire after 5 minutes (enforced by app code, but this
-- index makes the cleanup DELETE fast)
CREATE INDEX idx_event_mailbox_expiry ON event_mailbox(account_id, created_at);
