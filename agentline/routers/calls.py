"""
AgentLine — Calls Router
Initiate and manage voice calls.
"""

import secrets
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from agentline.auth_middleware import get_current_account
from agentline.database import get_db, get_db_conn
from agentline.models.call import CallRequest
from agentline.signalwire_client import initiate_call as signalwire_initiate_call
from agentline.signalwire_client import hangup_call as signalwire_hangup_call
from agentline.billing import check_balance, CALL_RATE_PER_MINUTE

# Minimum balance required to initiate a call (~5 minutes worth)
MIN_CALL_BALANCE = round(CALL_RATE_PER_MINUTE * 5, 2)  # $0.50

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/calls", tags=["Calls"])


@router.post("", operation_id="make_outbound_call")
async def create_call(
    body: CallRequest,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Make an outbound phone call from your AI agent.

    Initiates a real phone call from the AI agent's phone number to the
    specified destination. The agent uses its configured system prompt,
    voice, and greeting to conduct the conversation autonomously.

    The AI agent handles the entire call — speech-to-text, LLM reasoning,
    and text-to-speech — in real time. The call transcript is saved
    automatically and can be retrieved via GET /v1/calls/{call_id}/transcript.

    Request body:
      - agent_id: the AI agent making the call
      - to_number: destination phone number in E.164 format (e.g. "+12125551234")
      - from_number_id: (optional) specific number to call from
      - system_prompt: (optional) override the agent's default prompt for this call
      - initial_greeting: (optional) override the agent's greeting for this call
      - voice_id: (optional) override the voice for this call
    """
    agent = await db.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND account_id = $2",
        body.agent_id, account["id"],
    )
    if not agent:
        raise HTTPException(404, "Agent not found.")

    if body.from_number_id:
        number = await db.fetchrow(
            "SELECT * FROM phone_numbers WHERE id=$1 AND account_id=$2 AND status='active'",
            body.from_number_id, account["id"],
        )
    else:
        number = await db.fetchrow(
            "SELECT * FROM phone_numbers WHERE agent_id=$1 AND status='active' ORDER BY created_at LIMIT 1",
            body.agent_id,
        )
    if not number:
        raise HTTPException(400, "Agent has no active phone number.")

    # ── Billing: require minimum balance before initiating call ──
    try:
        await check_balance(db, account["id"], MIN_CALL_BALANCE)
    except ValueError as e:
        raise HTTPException(
            402,
            f"Insufficient balance to make a call. Minimum ${MIN_CALL_BALANCE:.2f} required. {e}",
        )

    call_id = f"call_{secrets.token_urlsafe(12)}"
    system_prompt = body.system_prompt or agent["system_prompt"]
    initial_greeting = body.initial_greeting  # Per-call override (None = use agent default)
    now = datetime.now(timezone.utc)

    await db.execute(
        """INSERT INTO calls (id, account_id, agent_id, number_id, direction,
           from_number, to_number, system_prompt, initial_greeting, voice_id, status, started_at)
           VALUES ($1,$2,$3,$4,'outbound',$5,$6,$7,$8,$9,'initiated',$10)""",
        call_id, account["id"], body.agent_id, number["id"],
        number["phone_number"], body.to_number, system_prompt,
        initial_greeting,
        body.voice_id,  # Per-call voice override (None = use agent/account default)
        now,
    )

    try:
        provider_call_id = await signalwire_initiate_call(
            from_number=number["phone_number"],
            to_number=body.to_number,
            call_id=call_id,
        )
    except Exception as e:
        await db.execute("UPDATE calls SET status='failed' WHERE id=$1", call_id)
        raise HTTPException(502, f"Failed to initiate call: {str(e)}")

    await db.execute(
        "UPDATE calls SET provider_call_id=$1, status='in-progress' WHERE id=$2",
        provider_call_id, call_id
    )

    return {
        "id": call_id, "agent_id": body.agent_id,
        "from_number": number["phone_number"], "to_number": body.to_number,
        "direction": "outbound", "status": "in-progress",
        "started_at": now.isoformat(),
    }


@router.get("", operation_id="list_calls")
async def list_calls(
    agent_id: str | None = Query(None, description="Filter calls by AI agent ID"),
    status: str | None = Query(None, description="Filter by call status: 'initiated', 'in-progress', 'completed', or 'failed'"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of calls to return (1-200)"),
    offset: int = Query(0, ge=0, description="Number of calls to skip for pagination"),
    account=Depends(get_current_account), db=Depends(get_db),
):
    """
    List voice calls made by your AI agents.

    Returns call history with optional filters by agent or status.
    Each entry includes direction (inbound/outbound), duration,
    phone numbers, and current status.

    Filters:
      - agent_id: only calls for a specific AI agent
      - status: "initiated", "in-progress", "completed", or "failed"
    """
    conditions = ["account_id = $1"]
    params: list = [account["id"]]
    idx = 2
    if agent_id:
        conditions.append(f"agent_id = ${idx}"); params.append(agent_id); idx += 1
    if status:
        conditions.append(f"status = ${idx}"); params.append(status); idx += 1
    where = " AND ".join(conditions)
    params.extend([limit, offset])
    rows = await db.fetch(
        f"SELECT * FROM calls WHERE {where} ORDER BY started_at DESC LIMIT ${idx} OFFSET ${idx+1}",
        *params,
    )
    return [dict(r) for r in rows]


@router.get("/{call_id}", operation_id="get_call_details")
async def get_call(call_id: str, account=Depends(get_current_account), db=Depends(get_db)):
    """
    Get full details of a specific voice call.

    Returns the call's metadata including direction, phone numbers,
    status, duration, AI agent configuration used, and the full
    conversation transcript between the AI agent and the caller.
    """
    row = await db.fetchrow(
        "SELECT * FROM calls WHERE id=$1 AND account_id=$2", call_id, account["id"],
    )
    if not row:
        raise HTTPException(404, "Call not found.")
    return dict(row)


@router.get("/{call_id}/transcript", operation_id="get_call_transcript")
async def get_transcript(call_id: str, account=Depends(get_current_account), db=Depends(get_db)):
    """
    Get the full conversation transcript for a call.

    Returns the complete speech-to-text transcript of the phone call,
    with each turn labeled by role ("human" for the caller, "assistant"
    for the AI agent). Useful for reviewing what was said on the call,
    extracting information, or auditing AI agent behavior.
    """
    row = await db.fetchrow(
        "SELECT transcript, status FROM calls WHERE id=$1 AND account_id=$2",
        call_id, account["id"],
    )
    if not row:
        raise HTTPException(404, "Call not found.")
    return {"call_id": call_id, "status": row["status"], "transcript": row["transcript"] or []}


@router.post("/{call_id}/speak", operation_id="speak_on_call")
async def speak_on_call(
    call_id: str,
    body: dict,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Send text to be spoken on an active call.

    The text will be spoken to the person on the phone within ~3 seconds.

    Body: {"text": "Sure, I can help you with that."}
    """
    text = body.get("text", "")
    if not text:
        raise HTTPException(400, "text is required")

    call = await db.fetchrow(
        "SELECT * FROM calls WHERE id=$1 AND account_id=$2",
        call_id, account["id"],
    )
    if not call:
        raise HTTPException(404, "Call not found.")
    if call["status"] not in ("in-progress", "initiated"):
        raise HTTPException(400, f"Call is {call['status']}, cannot speak on it.")

    await db.execute(
        """INSERT INTO call_responses (call_id, response_text, spoken, created_at)
           VALUES ($1, $2, false, now())""",
        call_id, text,
    )

    logger.info("Call %s — agent queued: %s", call_id, text[:80])
    return {"queued": True, "call_id": call_id, "text": text}


@router.post("/{call_id}/hangup", operation_id="hangup_call")
async def hangup_call(
    call_id: str,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Hang up an active phone call.

    Programmatically terminates an in-progress voice call. Use this
    when the AI agent needs to end the conversation, or to force-stop
    a call that is no longer needed. The call's final transcript and
    billing are processed automatically after hangup.
    """
    call = await db.fetchrow(
        "SELECT * FROM calls WHERE id=$1 AND account_id=$2",
        call_id, account["id"],
    )
    if not call:
        raise HTTPException(404, "Call not found.")
    if call["status"] == "completed":
        return {"call_id": call_id, "status": "completed", "message": "Call already ended."}

    provider_call_id = call.get("provider_call_id")
    if not provider_call_id:
        # No provider call ID means the call never connected — just mark completed
        await db.execute(
            "UPDATE calls SET status='completed', ended_at=now() WHERE id=$1",
            call_id,
        )
        return {"call_id": call_id, "status": "completed", "message": "Call was never connected, marked as completed."}

    try:
        await signalwire_hangup_call(provider_call_id)
    except Exception as e:
        logger.warning("Provider hangup failed for call %s: %s (marking completed anyway)", call_id, e)

    # Mark the call as completed in our DB
    await db.execute(
        "UPDATE calls SET status='completed', ended_at=now() WHERE id=$1 AND status!='completed'",
        call_id,
    )

    logger.info("Call %s — agent-initiated hangup", call_id)
    return {"call_id": call_id, "status": "completed", "message": "Call terminated."}


@router.get("/{call_id}/listen", operation_id="listen_to_call")
async def listen_from_call(
    call_id: str,
    wait: bool = Query(False, description="Long-poll: hold connection until new speech arrives (max 25s)"),
    after: int = Query(0, description="Only return transcript entries after this index"),
    account=Depends(get_current_account),
):
    """
    Get speech from the caller on an active call.
    
    Two modes:
    - **Instant** (default): Returns current transcript immediately
    - **Long-poll** (`?wait=true`): Holds connection up to 25 seconds 
      until new speech arrives, then returns it. Perfect for agents
      that want to react in real-time without webhooks.
    
    Use `?after=N` to only get transcript entries after index N,
    so you don't re-process old messages.
    """
    # Fetch current state (quick query, releases connection immediately)
    async with get_db_conn() as db:
        call = await db.fetchrow(
            "SELECT transcript, status FROM calls WHERE id=$1 AND account_id=$2",
            call_id, account["id"],
        )
    if not call:
        raise HTTPException(404, "Call not found.")

    if not wait:
        # Instant mode — return current state
        return _format_listen_response(call_id, call, after)

    # Long-poll mode — wait for new speech (up to 25 seconds)
    initial_len = _transcript_len(call.get("transcript"))

    for _ in range(25):  # Check once per second, max 25 seconds
        await asyncio.sleep(1)

        async with get_db_conn() as poll_db:
            call = await poll_db.fetchrow(
                "SELECT transcript, status FROM calls WHERE id=$1",
                call_id,
            )

        if not call:
            break

        current_len = _transcript_len(call.get("transcript"))

        # New speech arrived!
        if current_len > initial_len:
            return _format_listen_response(call_id, call, after)

        # Call ended
        if call["status"] == "completed":
            return _format_listen_response(call_id, call, after)

    # Timeout — return current state
    return _format_listen_response(call_id, call, after)


def _transcript_len(transcript) -> int:
    """Get the number of entries in a transcript."""
    if not transcript:
        return 0
    if isinstance(transcript, list):
        return len(transcript)
    try:
        return len(json.loads(transcript))
    except (json.JSONDecodeError, TypeError):
        return 0


def _format_listen_response(call_id: str, call, after: int = 0) -> dict:
    """Format the /listen response with transcript filtering."""
    transcript = call.get("transcript") or []
    if isinstance(transcript, str):
        try:
            transcript = json.loads(transcript)
        except (json.JSONDecodeError, TypeError):
            transcript = []

    # Filter to only new entries if after is specified
    new_entries = transcript[after:] if after > 0 else transcript

    # Get last human speech
    last_human = None
    for turn in reversed(transcript):
        if isinstance(turn, dict) and turn.get("role") == "human":
            last_human = turn
            break

    return {
        "call_id": call_id,
        "status": call["status"],
        "last_speech": last_human,
        "new_entries": new_entries,
        "total_turns": len(transcript),
        "transcript": transcript,
    }
