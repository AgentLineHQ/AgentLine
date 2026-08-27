"""
AgentLine — Agents Router
Full CRUD for agent configuration.
"""

import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from agentline.auth_middleware import get_current_account
from agentline.database import get_db
from agentline.models.agent import AgentCreate, AgentUpdate, AgentOut
from agentline.voice.voices import is_valid_voice_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agents", tags=["Agents"])


def _validate_voice_id(voice_id: str | None) -> None:
    """Reject voice IDs that would silently resolve to the default on calls."""
    if voice_id is not None and not is_valid_voice_id(voice_id):
        raise HTTPException(
            400,
            "Invalid voice_id. Use a preset name (see GET /v1/voices) or a "
            "Cartesia voice UUID.",
        )


@router.post("", response_model=AgentOut, operation_id="create_agent")
async def create_agent(
    body: AgentCreate,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Create a new AI voice agent for telephony.

    Sets up a new AI phone agent with a custom system prompt, voice,
    and greeting. Once created, buy a phone number and attach it to
    this agent so it can make and receive calls autonomously.

    Fields:
      - name: Display name for the agent
      - system_prompt: Instructions that define the agent's personality and behavior on calls
      - initial_greeting: What the AI agent says when the call connects
      - voice_id: TTS voice preset (e.g. "female-1") or Cartesia UUID
      - model_tier: LLM model tier — "turbo", "balanced", or "max"
      - transfer_number: Phone number to transfer calls to (e.g. a human operator)
      - voicemail_message: Message the agent leaves if the call goes to voicemail
      - owner_phone: Owner number — calls from it enter task mode
    """
    _validate_voice_id(body.voice_id)
    agent_id = f"agt_{secrets.token_urlsafe(12)}"
    now = datetime.now(timezone.utc)

    # Try INSERT with owner_phone; fall back without it if migration hasn't run yet.
    try:
        await db.execute(
            """INSERT INTO agents
               (id, account_id, name, system_prompt, initial_greeting,
                voice_id, model_tier, transfer_number, voicemail_message,
                owner_phone, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            agent_id,
            account["id"],
            body.name,
            body.system_prompt,
            body.initial_greeting,
            body.voice_id,
            body.model_tier,
            body.transfer_number,
            body.voicemail_message,
            body.owner_phone,
            now,
        )
    except Exception as e:
        if "owner_phone" in str(e):
            # Column doesn't exist yet — insert without it
            await db.execute(
                """INSERT INTO agents
                   (id, account_id, name, system_prompt, initial_greeting,
                    voice_id, model_tier, transfer_number, voicemail_message,
                    created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                agent_id,
                account["id"],
                body.name,
                body.system_prompt,
                body.initial_greeting,
                body.voice_id,
                body.model_tier,
                body.transfer_number,
                body.voicemail_message,
                now,
            )
        else:
            raise

    return AgentOut(
        id=agent_id,
        account_id=account["id"],
        name=body.name,
        system_prompt=body.system_prompt,
        initial_greeting=body.initial_greeting,
        voice_id=body.voice_id,
        model_tier=body.model_tier,
        transfer_number=body.transfer_number,
        voicemail_message=body.voicemail_message,
        owner_phone=body.owner_phone,
        created_at=now,
    )


@router.get("", operation_id="list_agents")
async def list_agents(
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    List all AI voice agents configured on your account.

    Returns every AI phone agent you've created, including their
    system prompts, voice settings, and associated phone numbers.
    Useful for checking which agents are ready to make or receive calls.
    """
    rows = await db.fetch(
        "SELECT * FROM agents WHERE account_id = $1 ORDER BY created_at DESC",
        account["id"],
    )
    return [dict(r) for r in rows]


@router.get("/{agent_id}", operation_id="get_agent")
async def get_agent(
    agent_id: str,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Get details of a specific AI voice agent.

    Returns the agent's full configuration including system prompt,
    voice settings, greeting, and transfer number.
    """
    row = await db.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND account_id = $2",
        agent_id,
        account["id"],
    )
    if not row:
        raise HTTPException(404, "Agent not found.")
    return dict(row)


@router.patch("/{agent_id}", operation_id="update_agent")
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Update an AI voice agent's configuration.

    Modify any combination of the agent's settings: system prompt,
    voice, greeting, transfer number, or voicemail message.
    Changes take effect on the next call the agent handles.
    Only include the fields you want to change — unset fields are preserved.
    """
    existing = await db.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND account_id = $2",
        agent_id,
        account["id"],
    )
    if not existing:
        raise HTTPException(404, "Agent not found.")

    # Build dynamic SET clause from non-None fields
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update.")

    if "voice_id" in updates:
        _validate_voice_id(updates["voice_id"])

    def _build_update_query(upd: dict):
        set_clauses = []
        values = []
        for i, (field, value) in enumerate(upd.items(), start=1):
            set_clauses.append(f"{field} = ${i}")
            values.append(value)
        values.append(agent_id)
        values.append(account["id"])
        query = f"""UPDATE agents
                    SET {', '.join(set_clauses)}
                    WHERE id = ${len(values) - 1} AND account_id = ${len(values)}
                    RETURNING *"""
        return query, values

    query, values = _build_update_query(updates)

    try:
        row = await db.fetchrow(query, *values)
    except Exception as e:
        if "owner_phone" in str(e) and "owner_phone" in updates:
            # Column doesn't exist yet — retry without it
            updates.pop("owner_phone")
            if not updates:
                raise HTTPException(400, "owner_phone is not yet available. Run the migration first.")
            query, values = _build_update_query(updates)
            row = await db.fetchrow(query, *values)
        else:
            raise
    return dict(row)


@router.delete("/{agent_id}", operation_id="delete_agent")
async def delete_agent(
    agent_id: str,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Delete an AI voice agent.

    Permanently removes the agent, its calls, messages, and conversations,
    and detaches any phone numbers assigned to it. Detached numbers remain
    active on your account and can be reassigned to another agent.
    """
    existing = await db.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND account_id = $2",
        agent_id,
        account["id"],
    )
    if not existing:
        raise HTTPException(404, "Agent not found.")

    # Detach numbers (don't release — let them be reassigned)
    await db.execute(
        "UPDATE phone_numbers SET agent_id = NULL WHERE agent_id = $1",
        agent_id,
    )

    # Remove dependents that reference agents(id) without a cascade FK.
    # Done in app code so agent deletion never hits a FK violation.
    # Non-fatal if it fails.
    try:
        await db.execute("DELETE FROM webhooks WHERE agent_id = $1", agent_id)
    except Exception as e:
        logger.warning("Non-fatal: could not delete webhook for agent %s: %s", agent_id[:12], e)

    # calls/messages/conversations hold plain FKs to agents(id) — delete them
    # (and everything cascading off calls) before the agent row itself.
    await db.execute("DELETE FROM calls WHERE agent_id = $1", agent_id)
    await db.execute("DELETE FROM messages WHERE agent_id = $1", agent_id)
    await db.execute("DELETE FROM conversations WHERE agent_id = $1", agent_id)

    await db.execute("DELETE FROM agents WHERE id = $1", agent_id)

    return {"deleted": True, "agent_id": agent_id}
