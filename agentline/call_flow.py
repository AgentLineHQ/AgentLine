"""Shared call and SMS lifecycle used by every telephony provider.

Provider webhooks parse their own form fields, then call these helpers.
Nothing here charges a balance or talks to Supabase.
"""

import json
import logging
import secrets
from datetime import datetime, timezone

from agentline.database import get_db_conn
from agentline.voice.runtime import CallContext, get_voice_runtime
from agentline.voice.voices import resolve_voice_chain, DEFAULT_VOICE_ID

logger = logging.getLogger(__name__)

# Status values that mean the call is still up. Everything else closes the row,
# including carrier-specific causes such as Plivo's NORMAL_CLEARING.
_STILL_OPEN = {"", "ringing", "in-progress", "queued", "initiated", "ring", "answered"}


def e164(number: str) -> str:
    number = (number or "").strip()
    if number and not number.startswith("+"):
        return f"+{number}"
    return number


def parse_transcript(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, list):
        return raw
    return []


def parse_duration(value) -> int:
    text = str(value or "0")
    return int(text) if text.isdigit() else 0


async def load_call_context(call_id: str, media: str) -> CallContext:
    """Load the prompt, voice, and runtime for an active call."""
    system_prompt = "You are a helpful voice assistant. Keep responses brief and conversational."
    initial_greeting = "Hello, how can I help you today?"
    voice_id = DEFAULT_VOICE_ID
    model_tier = "balanced"
    voice_runtime = None
    from_number = ""
    to_number = ""
    direction = ""
    provider_call_id = ""
    agent_id = None
    account_id = None
    voicemail_message = None
    relay_mode = False

    try:
        async with get_db_conn() as db:
            call = await db.fetchrow("SELECT * FROM calls WHERE id=$1", call_id)
            if call:
                from_number = call.get("from_number") or ""
                to_number = call.get("to_number") or ""
                direction = call.get("direction") or ""
                provider_call_id = call.get("provider_call_id") or ""
                agent_id = call.get("agent_id")
                account_id = call.get("account_id")
                agent = await db.fetchrow("SELECT * FROM agents WHERE id=$1", call["agent_id"])
                account = None
                if call.get("account_id"):
                    account = await db.fetchrow("SELECT * FROM accounts WHERE id=$1", call["account_id"])
                if agent:
                    system_prompt = agent.get("system_prompt") or system_prompt
                    initial_greeting = agent.get("initial_greeting") or initial_greeting
                    model_tier = agent.get("model_tier") or "balanced"
                    voice_runtime = agent.get("voice_runtime") or None
                    voicemail_message = agent.get("voicemail_message")
                if agent_id and account_id:
                    from agentline.voice.relay_store import get_relay_transport
                    from agentline.webhook_dispatcher import is_webhook_known_dead
                    relay_transport = await get_relay_transport(db, account_id, agent_id)
                    relay_mode = bool(
                        relay_transport
                        and not (relay_transport == "webhook" and is_webhook_known_dead(agent_id))
                    )
                voice_id = resolve_voice_chain(
                    per_call_voice=call.get("voice_id"),
                    agent_voice=agent.get("voice_id") if agent else None,
                    account_voice=account.get("default_voice_id") if account else None,
                )
                if call.get("system_prompt"):
                    system_prompt = call["system_prompt"]
                if call.get("initial_greeting"):
                    initial_greeting = call["initial_greeting"]
    except Exception as exc:
        logger.warning("Failed to load call context for %s: %s", call_id, exc)

    return CallContext(
        call_id=call_id,
        system_prompt=system_prompt,
        initial_greeting=initial_greeting,
        voice_id=voice_id,
        model_tier=model_tier,
        media=media,
        from_number=from_number,
        to_number=to_number,
        direction=direction,
        provider_call_id=provider_call_id,
        voice_runtime=voice_runtime,
        agent_id=agent_id,
        account_id=account_id,
        voicemail_message=voicemail_message,
        relay_mode=relay_mode,
    )


async def mark_answered(call_id: str, provider_call_id: str) -> None:
    if not provider_call_id:
        return
    async with get_db_conn() as db:
        await db.execute(
            "UPDATE calls SET provider_call_id=$1, status='in-progress' WHERE id=$2",
            provider_call_id,
            call_id,
        )


async def render_answer(provider, call_id: str) -> str:
    """Ask the voice runtime how to attach media, then render carrier XML."""
    ctx = await load_call_context(call_id, provider.media)
    runtime = get_voice_runtime(ctx.voice_runtime)
    plan = await runtime.prepare(ctx)
    if plan.mode == "sip" and plan.sip_uri:
        return provider.sip_dial_xml(plan.sip_uri, {
            "X-Agentline-Call-Id": call_id,
            "X-Agentline-Room": f"call-{call_id}",
        })
    if plan.xml:
        return plan.xml
    return provider.stream_xml(call_id)


async def run_media(websocket, call_id: str, media: str) -> None:
    ctx = await load_call_context(call_id, media)
    runtime = get_voice_runtime(ctx.voice_runtime)
    await runtime.run(websocket, ctx)


async def open_inbound_call(
    from_number: str,
    to_number: str,
    provider_call_id: str,
    provider_name: str,
) -> str | None:
    """Create the inbound call row. Returns None when the number is unknown."""
    from_number = e164(from_number)
    to_number = e164(to_number)
    async with get_db_conn() as db:
        number = await db.fetchrow(
            """SELECT * FROM phone_numbers
               WHERE (phone_number=$1 OR phone_number=$2) AND status='active'""",
            to_number,
            to_number.lstrip("+"),
        )
        if not number:
            return None
        agent = await db.fetchrow("SELECT * FROM agents WHERE id=$1", number["agent_id"])
        from agentline.voice.owner_mode import (
            OWNER_MODE_GREETING,
            build_owner_prompt,
            is_owner_number,
        )
        is_owner_call = bool(agent) and is_owner_number(agent, from_number)
        call_id = f"call_{secrets.token_urlsafe(12)}"
        await db.execute(
            """INSERT INTO calls
               (id, account_id, agent_id, number_id, provider, provider_call_id,
                direction, from_number, to_number, system_prompt, initial_greeting,
                status, started_at)
               VALUES ($1,$2,$3,$4,$5,$6,'inbound',$7,$8,$9,$10,'in-progress',now())""",
            call_id,
            number["account_id"],
            number["agent_id"],
            number["id"],
            provider_name,
            provider_call_id,
            from_number,
            to_number,
            build_owner_prompt(agent) if is_owner_call else (agent["system_prompt"] if agent else ""),
            OWNER_MODE_GREETING if is_owner_call else (agent.get("initial_greeting") if agent else None),
        )
    from agentline.event_bus import publish_event
    await publish_event(
        account_id=number["account_id"],
        agent_id=number["agent_id"],
        event_type="call.received",
        payload={
            "call_id": call_id,
            "agent_id": number["agent_id"],
            "number": to_number,
            "from": from_number,
            "direction": "inbound",
            "provider": provider_name,
            "is_owner_call": is_owner_call,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    return call_id


async def finalize_call(
    call_id: str | None,
    provider_call_id: str,
    status: str,
    duration_seconds: int,
) -> None:
    """Mark a call finished and drop a transcript event in the mailbox."""
    status_key = (status or "").strip().lower().replace(" ", "_")
    if status_key in _STILL_OPEN:
        return
    async with get_db_conn() as db:
        call = None
        if call_id:
            call = await db.fetchrow("SELECT * FROM calls WHERE id=$1", call_id)
        if not call and provider_call_id:
            call = await db.fetchrow(
                "SELECT * FROM calls WHERE provider_call_id=$1",
                provider_call_id,
            )
        if not call:
            logger.warning("Hangup: no call for id=%s sid=%s", call_id, provider_call_id)
            return
        call_id = call["id"]
        if (call.get("status") or "") in {"completed", "failed", "busy", "no-answer", "canceled"}:
            if duration_seconds > 0:
                await db.execute(
                    """UPDATE calls SET duration_seconds=$1, ended_at=COALESCE(ended_at, now())
                       WHERE id=$2 AND (duration_seconds IS NULL OR duration_seconds < $1)""",
                    duration_seconds,
                    call_id,
                )
            return
        stored_status = "failed" if status_key == "failed" else "completed"
        await db.execute(
            """UPDATE calls SET status=$1, duration_seconds=$2, ended_at=now()
               WHERE id=$3 AND status!='completed'""",
            stored_status,
            duration_seconds,
            call_id,
        )
        transcript = parse_transcript(call.get("transcript"))
        from agentline.voice.owner_mode import OWNER_MODE_SENTINEL
        is_owner_task = (call.get("system_prompt") or "").startswith(OWNER_MODE_SENTINEL)
        if is_owner_task and stored_status == "completed":
            event_type = "call.owner_task"
        elif stored_status == "completed":
            event_type = "call.completed"
        else:
            event_type = "call.failed"
    from agentline.event_bus import publish_event
    await publish_event(
        account_id=call.get("account_id"),
        agent_id=call.get("agent_id"),
        event_type=event_type,
        payload={
            "call_id": call_id,
            "status": status,
            "direction": call.get("direction", ""),
            "from_number": call.get("from_number", ""),
            "to_number": call.get("to_number", ""),
            "duration_seconds": duration_seconds,
            "transcript": transcript,
            "is_owner_task": is_owner_task,
        },
    )


async def store_inbound_sms(
    from_number: str,
    to_number: str,
    text: str,
    provider_message_id: str,
    media_url: str | None = None,
) -> dict | None:
    from_number = e164(from_number)
    to_number = e164(to_number)
    async with get_db_conn() as db:
        number = await db.fetchrow(
            "SELECT * FROM phone_numbers WHERE (phone_number=$1 OR phone_number=$2)",
            to_number,
            to_number.lstrip("+"),
        )
        if not number:
            return None
        conv = await db.fetchrow(
            "SELECT * FROM conversations WHERE number_id=$1 AND contact_number=$2",
            number["id"],
            from_number,
        )
        if not conv:
            conv_id = f"conv_{secrets.token_urlsafe(12)}"
            await db.execute(
                """INSERT INTO conversations
                   (id, account_id, agent_id, number_id, contact_number, last_message_at)
                   VALUES ($1,$2,$3,$4,$5,now())""",
                conv_id,
                number["account_id"],
                number["agent_id"],
                number["id"],
                from_number,
            )
        else:
            conv_id = conv["id"]
            await db.execute(
                "UPDATE conversations SET last_message_at = now() WHERE id = $1",
                conv_id,
            )
        msg_id = f"msg_{secrets.token_urlsafe(12)}"
        await db.execute(
            """INSERT INTO messages
               (id, account_id, agent_id, number_id, conversation_id,
                provider_message_id, direction, from_number, to_number, body, media_url)
               VALUES ($1,$2,$3,$4,$5,$6,'inbound',$7,$8,$9,$10)""",
            msg_id,
            number["account_id"],
            number["agent_id"],
            number["id"],
            conv_id,
            provider_message_id,
            from_number,
            to_number,
            text,
            media_url or None,
        )
    from agentline.event_bus import publish_event
    await publish_event(
        account_id=number["account_id"],
        agent_id=number["agent_id"],
        event_type="sms.received",
        payload={
            "message_id": msg_id,
            "conversation_id": conv_id,
            "agent_id": number["agent_id"],
            "number_id": number["id"],
            "from_number": from_number,
            "to_number": to_number,
            "body": text,
            "media_url": media_url or None,
        },
    )
    return {"message_id": msg_id}
