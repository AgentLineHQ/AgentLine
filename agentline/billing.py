"""
AgentLine — Billing Engine
Centralised billing logic: rate constants, balance checks, and ledger writes.

Rates:
  - Calls: $0.10 per minute (both inbound and outbound), billed per-second
  - Number provisioning: $2.00 per new number
"""

import math
import logging
from decimal import Decimal, ROUND_UP

logger = logging.getLogger(__name__)

# ── Rate constants (USD) ──────────────────────────────────────
CALL_RATE_PER_MINUTE = 0.10           # $0.10 / min for both directions
NUMBER_PROVISION_COST = 2.00          # $2.00 per new number


def calculate_call_cost(duration_seconds: int) -> float:
    """
    Calculate the cost of a call based on its duration.
    Billed per-second (pro-rated), rounded up to the nearest cent.
    Uses Decimal to avoid floating-point precision issues.

    Examples:
      30 seconds → $0.05
      60 seconds → $0.10
      90 seconds → $0.15
    """
    if duration_seconds <= 0:
        return 0.0
    cost = (Decimal(duration_seconds) / 60) * Decimal("0.10")
    # Round up to nearest cent
    return float(cost.quantize(Decimal("0.01"), rounding=ROUND_UP))


async def check_balance(db, account_id: str, required: float) -> float:
    """
    Check if an account has sufficient balance.
    Returns the current balance. Raises ValueError if insufficient.
    """
    balance = await db.fetchval(
        "SELECT balance FROM accounts WHERE id = $1", account_id
    )
    if balance is None:
        raise ValueError("Account not found.")
    balance = float(balance)
    if balance < required:
        raise ValueError(
            f"Insufficient balance. Current: ${balance:.2f}, required: ${required:.2f}"
        )
    return balance


async def debit_account(
    db,
    account_id: str,
    amount: float,
    txn_type: str,
    reference_id: str | None = None,
    description: str | None = None,
) -> float:
    """
    Atomically debit an account and write a ledger entry.
    Returns the new balance after the debit.

    Uses UPDATE ... RETURNING to guarantee atomicity — no race conditions.
    """
    if amount <= 0:
        raise ValueError("Debit amount must be positive.")

    # Atomic debit with balance floor check
    row = await db.fetchrow(
        """UPDATE accounts
           SET balance = balance - $1
           WHERE id = $2 AND balance >= $1
           RETURNING balance""",
        amount,
        account_id,
    )
    if row is None:
        # Either account doesn't exist or insufficient balance
        current = await db.fetchval(
            "SELECT balance FROM accounts WHERE id = $1", account_id
        )
        if current is None:
            raise ValueError("Account not found.")
        raise ValueError(
            f"Insufficient balance. Current: ${float(current):.2f}, required: ${amount:.2f}"
        )

    new_balance = float(row["balance"])

    # Write immutable ledger entry
    await db.execute(
        """INSERT INTO billing_ledger
           (account_id, amount, balance_after, txn_type, reference_id, description)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        account_id,
        -amount,           # negative = debit
        new_balance,
        txn_type,
        reference_id,
        description,
    )

    logger.info(
        "Billing: %s debited $%.4f (%s, ref=%s) → new balance $%.4f",
        account_id, amount, txn_type, reference_id, new_balance,
    )
    return new_balance


async def credit_account(
    db,
    account_id: str,
    amount: float,
    txn_type: str,
    reference_id: str | None = None,
    description: str | None = None,
) -> float:
    """
    Atomically credit an account and write a ledger entry.
    Returns the new balance after the credit.
    """
    if amount <= 0:
        raise ValueError("Credit amount must be positive.")

    row = await db.fetchrow(
        """UPDATE accounts
           SET balance = balance + $1
           WHERE id = $2
           RETURNING balance""",
        amount,
        account_id,
    )
    if row is None:
        raise ValueError("Account not found.")

    new_balance = float(row["balance"])

    await db.execute(
        """INSERT INTO billing_ledger
           (account_id, amount, balance_after, txn_type, reference_id, description)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        account_id,
        amount,            # positive = credit
        new_balance,
        txn_type,
        reference_id,
        description,
    )

    logger.info(
        "Billing: %s credited $%.4f (%s, ref=%s) → new balance $%.4f",
        account_id, amount, txn_type, reference_id, new_balance,
    )
    return new_balance
