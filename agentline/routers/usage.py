"""
AgentLine — Usage Router
Usage stats, billing metrics, balance, and transaction history.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from agentline.auth_middleware import get_current_account
from agentline.database import get_db
from agentline.billing import (
    CALL_RATE_PER_MINUTE,
    NUMBER_PROVISION_COST,
    calculate_call_cost,
    credit_account,
)

router = APIRouter(prefix="/v1/usage", tags=["Usage"])


@router.get("", operation_id="get_usage_stats")
async def get_usage(
    period: str = Query("current_month", description="Billing period: 'current_month', 'last_month', 'all_time', or 'YYYY-MM'"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Get usage statistics for your AI telephony account.

    Returns a summary of call volume, message counts, active phone
    numbers, current balance, and estimated costs for the billing period.
    Use this to monitor how actively your AI agents are using the
    telephony platform.
    """
    # Count calls
    call_stats = await db.fetchrow(
        """SELECT
            COUNT(*) AS total_calls,
            COUNT(*) FILTER (WHERE direction = 'outbound') AS outbound_calls,
            COUNT(*) FILTER (WHERE direction = 'inbound') AS inbound_calls,
            COALESCE(SUM(duration_seconds), 0) AS total_call_seconds
           FROM calls
           WHERE account_id = $1
             AND started_at >= date_trunc('month', now())""",
        account["id"],
    )

    # Count messages
    msg_stats = await db.fetchrow(
        """SELECT
            COUNT(*) AS total_messages,
            COUNT(*) FILTER (WHERE direction = 'outbound') AS outbound_messages,
            COUNT(*) FILTER (WHERE direction = 'inbound') AS inbound_messages
           FROM messages
           WHERE account_id = $1
             AND created_at >= date_trunc('month', now())""",
        account["id"],
    )

    # Count active numbers
    number_count = await db.fetchval(
        "SELECT COUNT(*) FROM phone_numbers WHERE account_id=$1 AND status='active'",
        account["id"],
    )

    # Get current balance
    balance = await db.fetchval(
        "SELECT balance FROM accounts WHERE id = $1", account["id"]
    )

    # Calculate estimated cost for this month's calls
    total_seconds = int(call_stats["total_call_seconds"]) if call_stats else 0
    estimated_call_cost = calculate_call_cost(total_seconds)

    return {
        "period": period,
        "balance": float(balance) if balance is not None else 0.0,
        "calls": dict(call_stats) if call_stats else {},
        "messages": dict(msg_stats) if msg_stats else {},
        "active_numbers": number_count or 0,
        "billing": {
            "estimated_call_cost_this_month": estimated_call_cost,
            "rates": {
                "call_per_minute": CALL_RATE_PER_MINUTE,
                "number_provision": NUMBER_PROVISION_COST,
            },
        },
    }


@router.get("/balance", operation_id="get_usage_balance")
async def get_balance(
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Get the current AI telephony account balance.

    Returns your available balance in USD. This balance is used to
    pay for AI agent phone calls and phone number provisioning.
    """
    balance = await db.fetchval(
        "SELECT balance FROM accounts WHERE id = $1", account["id"]
    )
    return {
        "account_id": account["id"],
        "balance": float(balance) if balance is not None else 0.0,
        "currency": "USD",
    }


@router.get("/transactions", operation_id="get_billing_transactions")
async def get_transactions(
    limit: int = Query(50, ge=1, le=200, description="Maximum number of transactions to return (1-200)"),
    offset: int = Query(0, ge=0, description="Number of transactions to skip for pagination"),
    txn_type: str | None = Query(None, description="Filter by transaction type: 'call_charge', 'number_provision', 'topup', or 'refund'"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Get billing transaction history for your AI telephony account.

    Returns a paginated list of all charges and credits including
    call charges, number provisioning fees, top-ups, and refunds.
    Most recent transactions are returned first.
    """
    conditions = ["account_id = $1"]
    params: list = [account["id"]]
    idx = 2

    if txn_type:
        conditions.append(f"txn_type = ${idx}")
        params.append(txn_type)
        idx += 1

    where = " AND ".join(conditions)
    params.extend([limit, offset])

    rows = await db.fetch(
        f"""SELECT id, amount, balance_after, txn_type, reference_id, description, created_at
            FROM billing_ledger
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}""",
        *params,
    )

    total = await db.fetchval(
        f"SELECT COUNT(*) FROM billing_ledger WHERE {where}",
        *params[:idx - 1],
    )

    return {
        "transactions": [
            {
                **dict(r),
                "amount": float(r["amount"]),
                "balance_after": float(r["balance_after"]),
            }
            for r in rows
        ],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


@router.post("/topup", operation_id="topup_balance")
async def topup_balance(
    body: dict,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Add funds to account balance (placeholder — integrate with Stripe/Razorpay).

    Body: {"amount": 10.00}
    """
    amount = body.get("amount", 0)
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise HTTPException(400, "amount must be a positive number.")
    if amount > 1000:
        raise HTTPException(400, "Maximum single top-up is $1000.")

    # TODO: Integrate with payment gateway (Stripe, Razorpay, etc.)
    # For now, this is a direct balance credit — only for testing/admin use.

    new_balance = await credit_account(
        db,
        account["id"],
        float(amount),
        txn_type="topup",
        description=f"Manual top-up of ${amount:.2f}",
    )

    return {
        "topped_up": float(amount),
        "new_balance": new_balance,
        "currency": "USD",
    }
