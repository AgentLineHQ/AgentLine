"""
AgentLine — Billing API Router
Dedicated endpoints for balance checking, expenditure tracking,
and post-call balance verification.

Endpoints:
  GET  /v1/billing/balance              — Current account balance
  GET  /v1/billing/expenditure          — Expenditure breakdown (calls, numbers, SMS)
  GET  /v1/billing/expenditure/calls    — Call-specific charges with per-call detail
  GET  /v1/billing/expenditure/numbers  — Number provisioning charges
  GET  /v1/billing/verify/{call_id}     — Verify balance was reduced after a specific call
  GET  /v1/billing/summary              — Month-over-month spending summary
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query

from agentline.auth_middleware import get_current_account
from agentline.database import get_db
from agentline.billing import (
    CALL_RATE_PER_MINUTE,
    NUMBER_PROVISION_COST,
    calculate_call_cost,
)

router = APIRouter(prefix="/v1/billing", tags=["Billing"])


# ────────────────────────────────────────────────────────────
# 1. Balance Check
# ────────────────────────────────────────────────────────────

@router.get("/balance", operation_id="get_account_balance")
async def get_balance(
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Get your AI telephony account balance and rate card.

    Returns the current balance, currency, billing rates for calls
    and phone numbers, and how many call minutes or phone numbers
    the balance can cover. Use this to check affordability before
    making calls or buying numbers for your AI agents.
    """
    row = await db.fetchrow(
        "SELECT balance, created_at FROM accounts WHERE id = $1",
        account["id"],
    )
    if not row:
        raise HTTPException(404, "Account not found.")

    balance = float(row["balance"])

    # Calculate how many minutes / numbers the balance can cover
    affordable_minutes = round(balance / CALL_RATE_PER_MINUTE, 1) if CALL_RATE_PER_MINUTE > 0 else 0
    affordable_numbers = int(balance // NUMBER_PROVISION_COST) if NUMBER_PROVISION_COST > 0 else 0

    return {
        "account_id": account["id"],
        "balance": balance,
        "currency": "USD",
        "account_since": row["created_at"].isoformat() if row["created_at"] else None,
        "affordable": {
            "call_minutes": affordable_minutes,
            "phone_numbers": affordable_numbers,
        },
        "rates": {
            "call_per_minute": CALL_RATE_PER_MINUTE,
            "number_provision": NUMBER_PROVISION_COST,
        },
    }


# ────────────────────────────────────────────────────────────
# 2. Expenditure (full breakdown)
# ────────────────────────────────────────────────────────────

@router.get("/expenditure", operation_id="get_expenditure_breakdown")
async def get_expenditure(
    period: str = Query(
        "current_month",
        description="Time period: 'current_month', 'last_month', 'all_time', or 'YYYY-MM'",
    ),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Get a detailed expenditure breakdown for AI telephony usage.

    Shows total spend split by category (voice calls, phone number
    provisioning, top-ups, refunds) with counts and averages.
    Useful for tracking how much your AI agents are spending on
    phone calls and phone numbers.
    """
    date_filter, date_params = _build_date_filter(period)

    # ── Total spend by category ──
    rows = await db.fetch(
        f"""SELECT
                txn_type,
                COUNT(*)                              AS txn_count,
                COALESCE(SUM(ABS(amount)), 0)         AS total_amount,
                COALESCE(AVG(ABS(amount)), 0)         AS avg_amount,
                MIN(created_at)                       AS first_txn,
                MAX(created_at)                       AS last_txn
            FROM billing_ledger
            WHERE account_id = $1 AND amount < 0
              {date_filter}
            GROUP BY txn_type
            ORDER BY total_amount DESC""",
        account["id"],
        *date_params,
    )

    categories = {}
    total_spent = 0.0
    for r in rows:
        amt = float(r["total_amount"])
        total_spent += amt
        categories[r["txn_type"]] = {
            "total": round(amt, 4),
            "count": r["txn_count"],
            "average": round(float(r["avg_amount"]), 4),
            "first_charge": r["first_txn"].isoformat() if r["first_txn"] else None,
            "last_charge": r["last_txn"].isoformat() if r["last_txn"] else None,
        }

    # ── Credits (top-ups, refunds) ──
    credit_rows = await db.fetch(
        f"""SELECT
                txn_type,
                COUNT(*)                      AS txn_count,
                COALESCE(SUM(amount), 0)      AS total_amount
            FROM billing_ledger
            WHERE account_id = $1 AND amount > 0
              {date_filter}
            GROUP BY txn_type""",
        account["id"],
        *date_params,
    )
    credits = {}
    total_credited = 0.0
    for r in credit_rows:
        amt = float(r["total_amount"])
        total_credited += amt
        credits[r["txn_type"]] = {
            "total": round(amt, 4),
            "count": r["txn_count"],
        }

    # ── Current balance ──
    balance = await db.fetchval(
        "SELECT balance FROM accounts WHERE id = $1", account["id"]
    )

    return {
        "period": period,
        "balance": float(balance) if balance else 0.0,
        "total_spent": round(total_spent, 4),
        "total_credited": round(total_credited, 4),
        "net_spend": round(total_spent - total_credited, 4),
        "breakdown": categories,
        "credits": credits,
        "currency": "USD",
    }


# ────────────────────────────────────────────────────────────
# 2a. Expenditure — Calls Only
# ────────────────────────────────────────────────────────────

@router.get("/expenditure/calls", operation_id="get_call_charges")
async def get_call_expenditure(
    limit: int = Query(50, ge=1, le=200, description="Maximum number of call charges to return (1-200)"),
    offset: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    List individual call charges from AI agent phone calls.

    Each entry includes the voice call duration, cost, direction
    (inbound/outbound), phone numbers involved, and timestamp.
    Shows exactly how much each AI agent call cost.
    """
    rows = await db.fetch(
        """SELECT
               bl.id, bl.amount, bl.balance_after, bl.reference_id,
               bl.description, bl.created_at AS charged_at,
               c.direction, c.from_number, c.to_number,
               c.duration_seconds, c.status AS call_status,
               c.started_at, c.ended_at
           FROM billing_ledger bl
           LEFT JOIN calls c ON c.id = bl.reference_id
           WHERE bl.account_id = $1
             AND bl.txn_type = 'call_charge'
           ORDER BY bl.created_at DESC
           LIMIT $2 OFFSET $3""",
        account["id"], limit, offset,
    )

    total = await db.fetchval(
        """SELECT COUNT(*) FROM billing_ledger
           WHERE account_id = $1 AND txn_type = 'call_charge'""",
        account["id"],
    )

    return {
        "call_charges": [
            {
                "ledger_id": r["id"],
                "call_id": r["reference_id"],
                "amount": abs(float(r["amount"])),
                "balance_after": float(r["balance_after"]),
                "direction": r["direction"],
                "from_number": r["from_number"],
                "to_number": r["to_number"],
                "duration_seconds": r["duration_seconds"],
                "call_status": r["call_status"],
                "description": r["description"],
                "charged_at": r["charged_at"].isoformat() if r["charged_at"] else None,
                "call_started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "call_ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
            }
            for r in rows
        ],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


# ────────────────────────────────────────────────────────────
# 2b. Expenditure — Numbers Only
# ────────────────────────────────────────────────────────────

@router.get("/expenditure/numbers", operation_id="get_number_charges")
async def get_number_expenditure(
    limit: int = Query(50, ge=1, le=200, description="Maximum number of number charges to return (1-200)"),
    offset: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    List phone number provisioning charges.

    Shows the cost of each phone number bought for your AI agents,
    including the number, country, and current status.
    """
    rows = await db.fetch(
        """SELECT
               bl.id, bl.amount, bl.balance_after, bl.reference_id,
               bl.description, bl.created_at AS charged_at,
               pn.phone_number, pn.country, pn.status AS number_status
           FROM billing_ledger bl
           LEFT JOIN phone_numbers pn ON pn.id = bl.reference_id
           WHERE bl.account_id = $1
             AND bl.txn_type = 'number_provision'
           ORDER BY bl.created_at DESC
           LIMIT $2 OFFSET $3""",
        account["id"], limit, offset,
    )

    total = await db.fetchval(
        """SELECT COUNT(*) FROM billing_ledger
           WHERE account_id = $1 AND txn_type = 'number_provision'""",
        account["id"],
    )

    return {
        "number_charges": [
            {
                "ledger_id": r["id"],
                "number_id": r["reference_id"],
                "phone_number": r["phone_number"],
                "country": r["country"],
                "number_status": r["number_status"],
                "amount": abs(float(r["amount"])),
                "balance_after": float(r["balance_after"]),
                "description": r["description"],
                "charged_at": r["charged_at"].isoformat() if r["charged_at"] else None,
            }
            for r in rows
        ],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


# ────────────────────────────────────────────────────────────
# 3. Verify Balance Deduction After a Call
# ────────────────────────────────────────────────────────────

@router.get("/verify/{call_id}", operation_id="verify_call_billing")
async def verify_call_deduction(
    call_id: str,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Verify that the balance was correctly reduced after a specific call.

    Returns the call details, the expected charge based on duration,
    the actual ledger entry, and a boolean `deducted` flag indicating
    whether the charge was applied correctly.

    Use this after a call completes to confirm billing accuracy.
    """
    # ── Fetch the call ──
    call = await db.fetchrow(
        "SELECT * FROM calls WHERE id = $1 AND account_id = $2",
        call_id, account["id"],
    )
    if not call:
        raise HTTPException(404, "Call not found.")

    duration = call.get("duration_seconds") or 0
    call_status = call.get("status", "unknown")
    expected_cost = calculate_call_cost(duration)

    # ── Find the corresponding ledger entry ──
    ledger = await db.fetchrow(
        """SELECT * FROM billing_ledger
           WHERE account_id = $1
             AND reference_id = $2
             AND txn_type = 'call_charge'
           ORDER BY created_at DESC LIMIT 1""",
        account["id"], call_id,
    )

    # ── Current balance ──
    current_balance = await db.fetchval(
        "SELECT balance FROM accounts WHERE id = $1", account["id"]
    )

    if ledger:
        actual_deducted = abs(float(ledger["amount"]))
        balance_after = float(ledger["balance_after"])
        deducted = True
        match = abs(actual_deducted - expected_cost) < 0.01  # within 1 cent
    else:
        actual_deducted = 0.0
        balance_after = None
        deducted = False
        match = False

    # Determine the reason if not deducted
    reason = None
    if not deducted:
        if call_status not in ("completed",):
            reason = f"Call status is '{call_status}' — charges are applied only when status is 'completed'."
        elif duration == 0:
            reason = "Call duration is 0 seconds — no charge applies."
        else:
            reason = "No ledger entry found — billing may have failed due to insufficient balance."

    return {
        "call_id": call_id,
        "call_status": call_status,
        "direction": call.get("direction"),
        "from_number": call.get("from_number"),
        "to_number": call.get("to_number"),
        "duration_seconds": duration,
        "expected_cost": expected_cost,
        "actual_deducted": actual_deducted,
        "deducted": deducted,
        "amounts_match": match,
        "balance_after_charge": balance_after,
        "current_balance": float(current_balance) if current_balance else 0.0,
        "reason": reason,
        "ledger_entry": {
            "id": ledger["id"],
            "amount": float(ledger["amount"]),
            "balance_after": float(ledger["balance_after"]),
            "created_at": ledger["created_at"].isoformat() if ledger["created_at"] else None,
            "description": ledger["description"],
        } if ledger else None,
        "currency": "USD",
    }


# ────────────────────────────────────────────────────────────
# 4. Monthly Spending Summary
# ────────────────────────────────────────────────────────────

@router.get("/summary", operation_id="get_spending_summary")
async def get_spending_summary(
    months: int = Query(6, ge=1, le=24, description="Number of months to show"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Month-over-month spending summary.

    Returns total debits grouped by month for trend analysis.
    """
    rows = await db.fetch(
        """SELECT
               date_trunc('month', created_at) AS month,
               txn_type,
               COUNT(*)                        AS txn_count,
               COALESCE(SUM(ABS(amount)), 0)   AS total_spent
           FROM billing_ledger
           WHERE account_id = $1
             AND amount < 0
             AND created_at >= (date_trunc('month', now()) - ($2 || ' months')::interval)
           GROUP BY month, txn_type
           ORDER BY month DESC, total_spent DESC""",
        account["id"], str(months),
    )

    # Group by month
    monthly: dict = {}
    for r in rows:
        month_key = r["month"].strftime("%Y-%m") if r["month"] else "unknown"
        if month_key not in monthly:
            monthly[month_key] = {"total": 0.0, "categories": {}}
        amt = float(r["total_spent"])
        monthly[month_key]["total"] = round(monthly[month_key]["total"] + amt, 4)
        monthly[month_key]["categories"][r["txn_type"]] = {
            "total": round(amt, 4),
            "count": r["txn_count"],
        }

    return {
        "months_shown": months,
        "monthly_spending": monthly,
        "currency": "USD",
    }


# ────────────────────────────────────────────────────────────
# Helper — Date filter builder
# ────────────────────────────────────────────────────────────

def _build_date_filter(period: str) -> tuple[str, list]:
    """
    Build SQL date filter clause and params for a period string.
    Returns (sql_fragment, params_list).
    The sql_fragment uses $2 as placeholder (since $1 is always account_id).
    """
    if period == "current_month":
        return "AND created_at >= date_trunc('month', now())", []
    elif period == "last_month":
        return (
            "AND created_at >= date_trunc('month', now()) - interval '1 month' "
            "AND created_at < date_trunc('month', now())"
        ), []
    elif period == "all_time":
        return "", []
    else:
        # Expect YYYY-MM format
        try:
            target = datetime.strptime(period, "%Y-%m")
            return (
                "AND created_at >= $2 AND created_at < ($2 + interval '1 month')"
            ), [target.replace(tzinfo=timezone.utc)]
        except ValueError:
            return "AND created_at >= date_trunc('month', now())", []
