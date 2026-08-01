"""
AgentLine — Billing Engine
Centralised billing logic: rate constants, balance checks, and ledger writes.

Rates:
  - Calls: $0.10 per minute (both inbound and outbound), billed per-second
  - Number provisioning: $2.00 per new number (first month)
  - Number monthly rental: $2.00 per active number each subsequent month
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP

logger = logging.getLogger(__name__)

# ── Rate constants (USD) ──────────────────────────────────────
CALL_RATE_PER_MINUTE = 0.10           # $0.10 / min for both directions
NUMBER_PROVISION_COST = 2.00          # $2.00 per new number (first month)
NUMBER_MONTHLY_COST = 2.00            # $2.00 per active number / month

# Don't spam the same account more than once per day on repeated debit failures
_LOW_BALANCE_EMAIL_COOLDOWN_SECONDS = 24 * 60 * 60


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


async def notify_low_balance(
    db,
    account_id: str,
    current_balance: float,
    required: float,
) -> None:
    """
    Email the account owner via Resend ("Low balance" template) when a debit
    cannot complete due to insufficient funds.

    Rate-limited to once per 24 hours per account so hourly monthly-billing
    retries do not spam the inbox.
    """
    try:
        row = await db.fetchrow(
            """SELECT human_email, last_low_balance_email_at
               FROM accounts
               WHERE id = $1""",
            account_id,
        )
        if not row:
            return

        email = row.get("human_email")
        if not email:
            logger.warning(
                "Low balance for account %s but no human_email on file", account_id
            )
            return

        last_sent = row.get("last_low_balance_email_at")
        if last_sent is not None:
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last_sent).total_seconds()
            if age < _LOW_BALANCE_EMAIL_COOLDOWN_SECONDS:
                logger.info(
                    "Skipping low-balance email for %s — last sent %.0fs ago",
                    account_id,
                    age,
                )
                return

        from agentline.email_client import send_low_balance_email

        result = await send_low_balance_email(
            email,
            balance=current_balance,
            required=required,
            account_id=account_id,
        )
        if result is not None:
            await db.execute(
                """UPDATE accounts
                   SET last_low_balance_email_at = now()
                   WHERE id = $1""",
                account_id,
            )
    except Exception as e:
        # Never let notification failures affect billing
        logger.error("notify_low_balance failed for %s: %s", account_id, e)


async def debit_account(
    db,
    account_id: str,
    amount: float,
    txn_type: str,
    reference_id: str | None = None,
    description: str | None = None,
    allow_negative: bool = False,
) -> float:
    """
    Atomically debit an account and write a ledger entry.
    Returns the new balance after the debit.

    Uses UPDATE ... RETURNING to guarantee atomicity — no race conditions.
    When allow_negative is True, the debit proceeds even if balance would
    go below zero. When balance is insufficient and allow_negative is False,
    sends a Resend "Low balance" email (rate-limited) then raises ValueError.
    """
    if amount <= 0:
        raise ValueError("Debit amount must be positive.")

    if allow_negative:
        row = await db.fetchrow(
            """UPDATE accounts
               SET balance = balance - $1
               WHERE id = $2
               RETURNING balance""",
            amount,
            account_id,
        )
        if row is None:
            raise ValueError("Account not found.")
    else:
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
            current_f = float(current)
            await notify_low_balance(db, account_id, current_f, amount)
            raise ValueError(
                f"Insufficient balance. Current: ${current_f:.2f}, required: ${amount:.2f}"
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


async def charge_due_monthly_number_fees(
    db,
    account_id: str | None = None,
) -> dict:
    """
    Charge $2.00 monthly rental for each active phone number whose last
    bill date (last_billed_at, else created_at) is at least one month ago.

    Designed for **lazy / on-request** billing (no cron or background
    scheduler). Call this for a single account when they hit the API
    (balance, numbers, calls, etc.). Safe on serverless (Vercel): runs
    only inside the request, then exits.

    Idempotent:
      - Only rows that claim the update via WHERE last_billed_at condition
        are charged
      - A recent number_monthly ledger entry also skips the charge

    Insufficient balance: debit fails, Resend low-balance email is sent
    (via debit_account), last_billed_at is rolled back for the next request.

    Returns a summary dict: charged, failed, skipped, total_amount.
    """
    if account_id:
        due = await db.fetch(
            """SELECT id, account_id, phone_number,
                      COALESCE(last_billed_at, created_at) AS last_bill
               FROM phone_numbers
               WHERE status = 'active'
                 AND account_id = $1
                 AND COALESCE(last_billed_at, created_at) <= (now() - interval '1 month')
               ORDER BY COALESCE(last_billed_at, created_at) ASC""",
            account_id,
        )
    else:
        due = await db.fetch(
            """SELECT id, account_id, phone_number,
                      COALESCE(last_billed_at, created_at) AS last_bill
               FROM phone_numbers
               WHERE status = 'active'
                 AND COALESCE(last_billed_at, created_at) <= (now() - interval '1 month')
               ORDER BY COALESCE(last_billed_at, created_at) ASC"""
        )

    charged = 0
    failed = 0
    skipped = 0
    total_amount = 0.0

    for row in due:
        number_id = row["id"]
        acct_id = row["account_id"]
        phone = row["phone_number"]

        # Extra idempotency: skip if already charged in the last ~28 days
        recent = await db.fetchval(
            """SELECT id FROM billing_ledger
               WHERE reference_id = $1
                 AND txn_type = 'number_monthly'
                 AND created_at > (now() - interval '28 days')
               LIMIT 1""",
            number_id,
        )
        if recent:
            # Align last_billed_at so we don't keep re-selecting this row
            await db.execute(
                """UPDATE phone_numbers
                   SET last_billed_at = now()
                   WHERE id = $1""",
                number_id,
            )
            skipped += 1
            continue

        # Claim the billing window first to reduce double-charge races
        claimed = await db.fetchrow(
            """UPDATE phone_numbers
               SET last_billed_at = now()
               WHERE id = $1
                 AND status = 'active'
                 AND COALESCE(last_billed_at, created_at) <= (now() - interval '1 month')
               RETURNING id""",
            number_id,
        )
        if not claimed:
            skipped += 1
            continue

        try:
            await debit_account(
                db,
                acct_id,
                NUMBER_MONTHLY_COST,
                txn_type="number_monthly",
                reference_id=number_id,
                description=f"Monthly rental for {phone}",
                allow_negative=False,
            )
            charged += 1
            total_amount += NUMBER_MONTHLY_COST
            logger.info(
                "Monthly number fee: $%.2f for %s (%s) on account %s",
                NUMBER_MONTHLY_COST, phone, number_id, acct_id,
            )
        except Exception as e:
            failed += 1
            # Roll back the claim so the next API request can retry
            await db.execute(
                """UPDATE phone_numbers
                   SET last_billed_at = $1
                   WHERE id = $2""",
                row["last_bill"],
                number_id,
            )
            logger.error(
                "Monthly number fee failed for %s (%s): %s",
                phone, number_id, e,
            )

    if charged or failed:
        logger.info(
            "Monthly number billing: charged=%d failed=%d skipped=%d total=$%.2f account=%s",
            charged, failed, skipped, total_amount, account_id or "*",
        )

    return {
        "charged": charged,
        "failed": failed,
        "skipped": skipped,
        "total_amount": round(total_amount, 4),
    }


async def apply_monthly_number_fees_for_account(db, account_id: str) -> dict:
    """
    Request-scoped helper: apply any due monthly number fees for one account.
    Never raises — failures are logged inside charge_due_monthly_number_fees.
    """
    try:
        return await charge_due_monthly_number_fees(db, account_id=account_id)
    except Exception as e:
        logger.warning(
            "apply_monthly_number_fees_for_account(%s) failed: %s", account_id, e
        )
        return {"charged": 0, "failed": 0, "skipped": 0, "total_amount": 0.0}


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
