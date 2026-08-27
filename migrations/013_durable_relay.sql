-- Durable, turn-correlated live context and outbound agent connections.
CREATE TABLE IF NOT EXISTS relay_turns (
    call_id         TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_id         TEXT NOT NULL,
    account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    agent_id        TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    push_token_hash TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'waiting',
    context         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    context_at      TIMESTAMPTZ,
    consumed_at     TIMESTAMPTZ,
    PRIMARY KEY (call_id, turn_id)
);

CREATE INDEX IF NOT EXISTS idx_relay_turns_waiting
    ON relay_turns(call_id, created_at) WHERE state = 'waiting';

CREATE TABLE IF NOT EXISTS agent_connections (
    agent_id      TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    account_id    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    connection_id TEXT NOT NULL,
    runtime       TEXT NOT NULL DEFAULT 'unknown',
    expires_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_connections_active
    ON agent_connections(account_id, expires_at);
