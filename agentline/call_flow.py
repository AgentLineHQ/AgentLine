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

    try:
        async with get_db_conn() as db:
            call = await db.fetchrow("SELECT * FROM calls WHERE id=$1", call_id)
            if call:
                from_number = call.get("from_number") or ""
                to_number = call.get("to_number") or ""
                direction = call.get("direction") or ""
                provider_call_id = call.get("provider_call_id") or ""
                agent = await db.fetchrow("SELECT * FROM agents WHERE id=$1", call["agent_id"])
                account = None
                if call.get("account_id"):
                    account = await db.fetchrow("SELECT * FROM accounts WHERE id=$1", call["account_id"])
                if agent:
                    system_prompt = agent.get("system_prompt") or system_prompt
                    initial_greeting = agent.get("initial_greeting") or initial_greeting
                    model_tier = agent.get("model_tier") or "balanced"
                    voice_runtime = agent.get("voice_runtime") or None
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
        call_id = f"call_{secrets.token_urlsafe(12)}"
        await db.execute(
            """INSERT INTO calls
               (id, account_id, agent_id, number_id, provider, provider_call_id,
                direction, from_number, to_number, system_prompt, status, started_at)
               VALUES ($1,$2,$3,$4,$5,$6,'inbound',$7,$8,$9,'in-progress',now())""",
            call_id,
            number["account_id"],
            number["agent_id"],
            number["id"],
            provider_name,
            provider_call_id,
            from_number,
            to_number,
            agent["system_prompt"] if agent else "",
        )
        event_id = f"evt_{secrets.token_urlsafe(12)}"
        payload = {
            "call_id": call_id,
            "agent_id": number["agent_id"],
            "number": to_number,
            "from": from_number,
            "direction": "inbound",
            "provider": provider_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await db.execute(
                """INSERT INTO event_mailbox
                   (event_id, account_id, agent_id, event_type, payload)
                   VALUES ($1, $2, $3, 'call.received', $4)""",
                event_id,
                number["account_id"],
                number["agent_id"],
                json.dumps(payload),
            )
        except Exception as exc:
            logger.error("Failed to push call.received for %s: %s", call_id, exc)
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
        stored_status = "failed" if status_key == "failed" else "completed"
        await db.execute(
            """UPDATE calls SET status=$1, duration_seconds=$2, ended_at=now()
               WHERE id=$3 AND status!='completed'""",
            stored_status,
            duration_seconds,
            call_id,
        )
        transcript = parse_transcript(call.get("transcript"))
        event_type = "call.completed" if stored_status == "completed" else "call.failed"
        event_id = f"evt_{secrets.token_urlsafe(12)}"
        try:
            await db.execute(
                """INSERT INTO event_mailbox
                   (event_id, account_id, agent_id, event_type, payload)
                   VALUES ($1, $2, $3, $4, $5)""",
                event_id,
                call.get("account_id"),
                call.get("agent_id"),
                event_type,
                json.dumps({
                    "call_id": call_id,
                    "status": status,
                    "direction": call.get("direction", ""),
                    "from_number": call.get("from_number", ""),
                    "to_number": call.get("to_number", ""),
                    "duration_seconds": duration_seconds,
                    "transcript": transcript,
                }),
            )
        except Exception as exc:
            logger.error("Failed to push %s for call %s: %s", event_type, call_id, exc)


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
        event_id = f"evt_{secrets.token_urlsafe(12)}"
        event_payload = {
            "message_id": msg_id,
            "conversation_id": conv_id,
            "from_number": from_number,
            "to_number": to_number,
            "body": text,
            "media_url": media_url or None,
        }
        try:
            await db.execute(
                """INSERT INTO event_mailbox
                   (event_id, account_id, agent_id, event_type, payload)
                   VALUES ($1, $2, $3, 'sms.received', $4)""",
                event_id,
                number["account_id"],
                number["agent_id"],
                json.dumps(event_payload),
            )
        except Exception as exc:
            logger.error("Failed to push sms.received for %s: %s", msg_id, exc)

    try:
        from agentline.webhook_dispatcher import dispatch_webhook
        await dispatch_webhook(number["account_id"], number["agent_id"], {
            "event": "sms.received",
            "message_id": msg_id,
            "conversation_id": conv_id,
            "agent_id": number["agent_id"],
            "number_id": number["id"],
            "from_number": from_number,
            "to_number": to_number,
            "body": text,
            "media_url": media_url or None,
        })
    except Exception as exc:
        logger.warning("Webhook dispatch failed for inbound SMS %s: %s", msg_id, exc)
    return {"message_id": msg_id}
