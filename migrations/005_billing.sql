-- Migration 005: Add billing support
-- Adds account balance and a billing ledger for auditable transactions.

-- Add balance column to accounts (default $10.00 starting credit)
ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS balance NUMERIC(12,4) NOT NULL DEFAULT 10.0000;

-- Billing ledger: every debit/credit is an immutable row
CREATE TABLE IF NOT EXISTS billing_ledger (
    id              SERIAL PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amount          NUMERIC(12,4) NOT NULL,   -- negative = debit, positive = credit
    balance_after   NUMERIC(12,4) NOT NULL,   -- snapshot of balance after this txn
    txn_type        TEXT NOT NULL,             -- 'call_charge', 'number_provision', 'topup', 'refund', etc.
    reference_id    TEXT,                      -- call_id or number_id or payment_id
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_billing_ledger_account
    ON billing_ledger(account_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_billing_ledger_type
    ON billing_ledger(account_id, txn_type);
