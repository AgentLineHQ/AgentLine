"""
AgentLine — Numbers Router
Phone number provisioning, listing, attachment, and reassignment.
"""

import secrets
import logging

from fastapi import APIRouter, Depends, HTTPException

from agentline.auth_middleware import get_current_account
from agentline.database import get_db
from agentline.models.number import NumberProvision, NumberOut
from agentline.signalwire_client import (
    provision_number as signalwire_provision_number,
    release_number as signalwire_release_number,
    configure_number_webhooks as signalwire_configure_webhooks,
)
from agentline.billing import (
    NUMBER_PROVISION_COST,
    check_balance,
    debit_account,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/numbers", tags=["Numbers"])




@router.post("", operation_id="buy_phone_number")
async def provision(
    body: NumberProvision,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Buy a US phone number for your AI agent.

    Searches for and purchases a real US phone number from the telephony
    provider, then attaches it to the specified AI agent. Once attached,
    the agent can make outbound calls and receive inbound calls on this number.

    Each AI agent can only have ONE active phone number. Costs $2.00 per number.

    Request body:
      - agent_id: str (required) — the AI agent to assign this number to
      - country: str (must be "US")
      - number_type: "local" | "tollfree"
      - area_code: preferred 3-digit US area code (e.g. "212" for NYC, "415" for SF)
    """
    if body.country.upper() != "US":
        raise HTTPException(400, "Only US numbers are supported.")

    # Verify agent belongs to this account
    agent = await db.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND account_id = $2",
        body.agent_id,
        account["id"],
    )
    if not agent:
        raise HTTPException(404, "Agent not found.")

    # Enforce one number per agent
    existing = await db.fetchrow(
        "SELECT id, phone_number FROM phone_numbers WHERE agent_id = $1 AND status = 'active'",
        body.agent_id,
    )
    if existing:
        raise HTTPException(
            409,
            f"Agent already has an active number: {existing['phone_number']} (id: {existing['id']}). "
            "Reassign it first with PATCH /v1/numbers/{number_id}/reassign before provisioning a new one.",
        )

    # Check balance before provisioning ($2.00 per number)
    try:
        await check_balance(db, account["id"], NUMBER_PROVISION_COST)
    except ValueError as e:
        raise HTTPException(402, str(e))

    # Provision via SignalWire (area_code takes priority over pattern)
    try:
        number_data = await signalwire_provision_number(
            country=body.country,
            number_type=body.number_type,
            area_code=body.area_code,
            pattern=body.pattern,
            agent_id=body.agent_id,
        )
    except Exception as e:
        raise HTTPException(502, f"Failed to provision number: {str(e)}")

    # Save to database
    number_id = f"num_{secrets.token_urlsafe(12)}"
    try:
        await db.execute(
            """INSERT INTO phone_numbers
               (id, account_id, agent_id, provider_id, phone_number, country, status)
               VALUES ($1, $2, $3, $4, $5, $6, 'active')""",
            number_id,
            account["id"],
            body.agent_id,
            number_data["provider_id"],
            number_data["phone_number"],
            body.country,
        )
        logger.info(
            "Number %s (%s) saved to DB for agent %s",
            number_id, number_data["phone_number"], body.agent_id,
        )
    except Exception as e:
        logger.error(
            "DB INSERT failed for number %s: %s — number was bought but NOT saved!",
            number_data["phone_number"], e,
        )
        # Try to release the number we just bought since DB save failed
        try:
            await signalwire_release_number(number_data["provider_id"])
        except Exception:
            pass
        raise HTTPException(
            500,
            f"Number {number_data['phone_number']} was provisioned on SignalWire but failed to save to database: {e}. "
            "The number has been released. Please try again.",
        )

    # Verify it was actually saved
    verify = await db.fetchrow("SELECT id FROM phone_numbers WHERE id = $1", number_id)
    if not verify:
        logger.error("Number %s INSERT succeeded but verification SELECT returned nothing!", number_id)
        raise HTTPException(500, "Database write verification failed. Please try again.")

    # Debit $2.00 for the provisioned number
    new_balance = None
    try:
        new_balance = await debit_account(
            db,
            account["id"],
            NUMBER_PROVISION_COST,
            txn_type="number_provision",
            reference_id=number_id,
            description=f"Provisioned number {number_data['phone_number']}",
        )
    except ValueError as e:
        # This shouldn't happen since we checked earlier, but handle gracefully
        logger.error("Balance debit failed after provisioning %s: %s", number_id, e)
        # Don't rollback the number — it's provisioned. Just log the billing failure.

    return {
        "id": number_id,
        "agent_id": body.agent_id,
        "phone_number": number_data["phone_number"],
        "country": body.country,
        "number_type": body.number_type,
        "status": "active",
        "cost": NUMBER_PROVISION_COST,
        "balance_remaining": new_balance,
    }


@router.get("", operation_id="list_phone_numbers")
async def list_numbers(
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    List all phone numbers provisioned on your account.

    Returns every phone number you've bought for your AI agents,
    including which agent each number is assigned to, the number's
    status (active/released), and country.
    """
    rows = await db.fetch(
        """SELECT * FROM phone_numbers
           WHERE account_id = $1
           ORDER BY created_at DESC""",
        account["id"],
    )
    return [dict(r) for r in rows]


@router.get("/{number_id}", operation_id="get_phone_number")
async def get_number(
    number_id: str,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Get details of a specific phone number.

    Returns the phone number, its assigned AI agent, provider ID,
    country, and current status.
    """
    row = await db.fetchrow(
        "SELECT * FROM phone_numbers WHERE id = $1 AND account_id = $2",
        number_id,
        account["id"],
    )
    if not row:
        raise HTTPException(404, "Number not found.")
    return dict(row)


@router.post("/attach", operation_id="attach_existing_number")
async def attach_existing_number(
    phone_number: str,
    agent_id: str,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Manually attach a number that was bought directly from SignalWire dashboard.
    Each agent can only have ONE active number.

    Query params:
      - phone_number: E.164 format (e.g. "+12125551234")
      - agent_id: agent to attach to
    """
    agent = await db.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND account_id = $2",
        agent_id,
        account["id"],
    )
    if not agent:
        raise HTTPException(404, "Agent not found.")

    # Enforce one number per agent
    existing_agent_num = await db.fetchrow(
        "SELECT id, phone_number FROM phone_numbers WHERE agent_id = $1 AND status = 'active'",
        agent_id,
    )
    if existing_agent_num:
        raise HTTPException(
            409,
            f"Agent already has an active number: {existing_agent_num['phone_number']}. "
            "Reassign it first with PATCH /v1/numbers/{number_id}/reassign before attaching a new one.",
        )

    # Check if this phone number is already attached
    existing = await db.fetchrow(
        "SELECT id FROM phone_numbers WHERE phone_number = $1 AND status = 'active'",
        phone_number,
    )
    if existing:
        raise HTTPException(409, f"Number {phone_number} is already attached (id: {existing['id']}).")

    if not phone_number.startswith("+1"):
        raise HTTPException(400, "Only US numbers (+1) via SignalWire are supported.")

    # ── Billing: check balance before attaching ($2.00 per number) ──
    try:
        await check_balance(db, account["id"], NUMBER_PROVISION_COST)
    except ValueError as e:
        raise HTTPException(402, str(e))

    number_id = f"num_{secrets.token_urlsafe(12)}"
    provider_id = phone_number.lstrip("+")

    try:
        await db.execute(
            """INSERT INTO phone_numbers
               (id, account_id, agent_id, provider_id, phone_number, country, status)
               VALUES ($1, $2, $3, $4, $5, $6, 'active')""",
            number_id,
            account["id"],
            agent_id,
            provider_id,
            phone_number,
            "US",
        )
        logger.info("Manually attached number %s to agent %s", phone_number, agent_id)
    except Exception as e:
        logger.error("Failed to attach number %s: %s", phone_number, e)
        raise HTTPException(500, f"Failed to save number to database: {e}")

    # ── Billing: debit $2.00 for the attached number ──
    try:
        await debit_account(
            db,
            account["id"],
            NUMBER_PROVISION_COST,
            txn_type="number_provision",
            reference_id=number_id,
            description=f"Attached existing number {phone_number}",
        )
    except ValueError as e:
        logger.error("Balance debit failed after attaching %s: %s", number_id, e)

    # Auto-configure webhook URLs on SignalWire
    webhook_status = "manual_config_needed"
    try:
        await signalwire_configure_webhooks(provider_id)
        webhook_status = "auto_configured"
    except Exception as e:
        logger.warning("Could not auto-configure webhooks for %s: %s", phone_number, e)

    return {
        "id": number_id,
        "agent_id": agent_id,
        "phone_number": phone_number,
        "status": "active",
        "cost": NUMBER_PROVISION_COST,
        "webhooks": webhook_status,
    }


@router.patch("/{number_id}/reassign", operation_id="reassign_number")
async def reassign_number(
    number_id: str,
    agent_id: str,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Reassign a phone number to a different AI agent.

    Moves an existing phone number from one AI agent to another.
    The target agent must not already have an active number assigned.
    The phone number remains active — only the agent ownership changes.
    """
    number = await db.fetchrow(
        "SELECT * FROM phone_numbers WHERE id = $1 AND account_id = $2",
        number_id,
        account["id"],
    )
    if not number:
        raise HTTPException(404, "Number not found.")

    agent = await db.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND account_id = $2",
        agent_id,
        account["id"],
    )
    if not agent:
        raise HTTPException(404, "Agent not found.")

    # Enforce one number per agent on the target
    existing = await db.fetchrow(
        "SELECT id, phone_number FROM phone_numbers WHERE agent_id = $1 AND status = 'active'",
        agent_id,
    )
    if existing:
        raise HTTPException(
            409,
            f"Target agent already has an active number: {existing['phone_number']}.",
        )

    await db.execute(
        "UPDATE phone_numbers SET agent_id = $1 WHERE id = $2",
        agent_id,
        number_id,
    )

    return {"number_id": number_id, "agent_id": agent_id, "reassigned": True}


