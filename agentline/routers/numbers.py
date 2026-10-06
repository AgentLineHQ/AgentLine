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
from agentline.providers.registry import get_telephony

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/numbers", tags=["Numbers"])




@router.post("", operation_id="buy_phone_number")
async def provision(
    body: NumberProvision,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Buys a phone number from the configured telephony provider and attaches
    it to the AI agent. The carrier decides which countries and area codes
    it can sell. You pay that carrier directly.

    Each AI agent can only have ONE active phone number.

    Request body:
      - agent_id: str (required) — the AI agent to assign this number to
      - country: ISO country code the provider should search
      - number_type: "local" | "tollfree"
      - area_code: preferred area code, when the provider supports it
    """
    provider = get_telephony()

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

    try:
        number_data = await provider.provision_number(
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
               (id, account_id, agent_id, provider_id, phone_number, country, status, provider)
               VALUES ($1, $2, $3, $4, $5, $6, 'active', $7)""",
            number_id,
            account["id"],
            body.agent_id,
            number_data.provider_id,
            number_data.phone_number,
            body.country,
            provider.name,
        )
        logger.info(
            "Number %s (%s) saved to DB for agent %s",
            number_id, number_data.phone_number, body.agent_id,
        )
    except Exception as e:
        logger.error(
            "DB INSERT failed for number %s: %s — number was bought but NOT saved!",
            number_data.phone_number, e,
        )
        try:
            await provider.release_number(number_data.provider_id)
        except Exception:
            pass
        raise HTTPException(
            500,
            f"Number {number_data.phone_number} was provisioned on {provider.name} but failed to save to the database: {e}. "
            "The number has been released. Please try again.",
        )

    # Verify it was actually saved
    verify = await db.fetchrow("SELECT id FROM phone_numbers WHERE id = $1", number_id)
    if not verify:
        logger.error("Number %s INSERT succeeded but verification SELECT returned nothing!", number_id)
        raise HTTPException(500, "Database write verification failed. Please try again.")

    return {
        "id": number_id,
        "agent_id": body.agent_id,
        "phone_number": number_data.phone_number,
        "country": body.country,
        "number_type": body.number_type,
        "status": "active",
        "provider": provider.name,
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
    Attach a number that was bought in the carrier's own dashboard.
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

    provider = get_telephony()
    number_id = f"num_{secrets.token_urlsafe(12)}"
    provider_id = phone_number.lstrip("+")

    try:
        await db.execute(
            """INSERT INTO phone_numbers
               (id, account_id, agent_id, provider_id, phone_number, country, status, provider)
               VALUES ($1, $2, $3, $4, $5, $6, 'active', $7)""",
            number_id,
            account["id"],
            agent_id,
            provider_id,
            phone_number,
            "US",
            provider.name,
        )
        logger.info("Manually attached number %s to agent %s", phone_number, agent_id)
    except Exception as e:
        logger.error("Failed to attach number %s: %s", phone_number, e)
        raise HTTPException(500, f"Failed to save number to database: {e}")

    webhook_status = "manual_config_needed"
    try:
        await provider.configure_number(provider_id)
        webhook_status = "auto_configured"
    except Exception as e:
        logger.warning("Could not auto-configure webhooks for %s: %s", phone_number, e)

    return {
        "id": number_id,
        "agent_id": agent_id,
        "phone_number": phone_number,
        "status": "active",
        "provider": provider.name,
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


