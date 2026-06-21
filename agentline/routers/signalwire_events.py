"""
AgentLine — SignalWire Events Router (WebSocket Streaming Pipeline)
Voice Architecture for US numbers:
  STT: Deepgram Nova-2 via WebSocket ($0.006/min — 90% cheaper than SignalWire <Gather>)
  LLM: Internal Hosted LLM (GPT-4o-mini / GPT-4o)
  TTS: Cartesia Sonic via API ($0.002/min — comparable to SignalWire <Say>)

  Flow: SignalWire <Connect><Stream> → WebSocket → Deepgram STT
        → LLM → Cartesia TTS → WebSocket → caller hears response.

  Previous architecture used <Gather input="speech"> + <Say> which cost
  $0.20/2min. New architecture costs ~$0.075/2min (63% savings).
"""

import asyncio
import secrets
import json
import logging
from datetime import datetime, timezone

import httpx

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from agentline.config import settings
from agentline.database import get_db_conn
from agentline.voice.pipeline import run_pipeline
from agentline.voice.voices import resolve_voice_chain, DEFAULT_VOICE_ID
from agentline.billing import calculate_call_cost, debit_account
from agentline.signalwire_client import _get_auth, _get_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signalwire", tags=["SignalWire Events"])

def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")

def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

def _parse_transcript(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except:
            return []
    if isinstance(raw, list):
        return raw
    return []


def _stream_xml(call_id: str) -> str:
    """
    Generate XML: connect to our WebSocket streaming pipeline.

    Uses <Connect><Stream> for bidirectional audio streaming.
    Audio goes to Deepgram STT (not SignalWire's expensive $0.0675/min STT).
    """
    # Use wss:// for production, ws:// for local development
    base = settings.base_url_clean
    if base.startswith("https://"):
        ws_base = base.replace("https://", "wss://")
    elif base.startswith("http://"):
        ws_base = base.replace("http://", "ws://")
    else:
        ws_base = f"wss://{base}"

    stream_url = f"{ws_base}/signalwire/stream/{call_id}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{stream_url}" />
    </Connect>
</Response>"""


# ────────────────────────────────────────────────────────────
# Voice — WebSocket Streaming Pipeline (Deepgram STT + Cartesia TTS)
# This replaces the expensive <Gather>+<Say> pattern
# ────────────────────────────────────────────────────────────

@router.websocket("/stream/{call_id}")
async def signalwire_stream(websocket: WebSocket, call_id: str):
    """
    WebSocket endpoint for SignalWire <Connect><Stream>.

    Receives raw mulaw audio from SignalWire, processes it through:
      Deepgram STT → LLM → Cartesia TTS
    and sends audio back to the caller.

    This replaces SignalWire's built-in <Gather> + <Say> and saves ~63% on costs.
    """
    await websocket.accept()
    logger.info("WebSocket stream connected for call %s", call_id)

    # Load call and agent context
    system_prompt = "You are a helpful voice assistant. Keep responses brief and conversational."
    initial_greeting = "Hello, how can I help you today?"
    voice_id = DEFAULT_VOICE_ID
    model_tier = "balanced"

    try:
        async with get_db_conn() as db:
            call = await db.fetchrow("SELECT * FROM calls WHERE id=$1", call_id)
            if call:
                agent = await db.fetchrow("SELECT * FROM agents WHERE id=$1", call["agent_id"])

                # Load account for default_voice_id
                account = await db.fetchrow(
                    "SELECT * FROM accounts WHERE id=$1", call["account_id"]
                ) if call.get("account_id") else None

                if agent:
                    system_prompt = agent.get("system_prompt") or system_prompt
                    initial_greeting = agent.get("initial_greeting") or initial_greeting
                    model_tier = agent.get("model_tier") or "balanced"


                # Voice resolution chain: per-call → agent → account → default
                voice_id = resolve_voice_chain(
                    per_call_voice=call.get("voice_id"),
                    agent_voice=agent.get("voice_id") if agent else None,
                    account_voice=account.get("default_voice_id") if account else None,
                )

                if call.get("system_prompt"):
                    # Per-call system prompt override
                    system_prompt = call["system_prompt"]

                if call.get("initial_greeting"):
                    # Per-call greeting override (outbound calls)
                    initial_greeting = call["initial_greeting"]
    except Exception as e:
        logger.warning("Failed to load agent context for call %s: %s", call_id, e)

    try:
        await run_pipeline(
            provider_ws=websocket,
            call_id=call_id,
            system_prompt=system_prompt,
            initial_greeting=initial_greeting,
            voice_id=voice_id,
            model_tier=model_tier,
            provider="signalwire",
        )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for call %s", call_id)
    except Exception as e:
        logger.error("Pipeline error for call %s: %s", call_id, e)
    finally:
        logger.info("WebSocket stream ended for call %s", call_id)


# ────────────────────────────────────────────────────────────
# Voice — Outbound Call Answered
# ────────────────────────────────────────────────────────────

@router.post("/answer/{call_id}", operation_id="signalwire_answer")
async def signalwire_answer(request: Request, call_id: str):
    """Call answered — connect to our streaming pipeline via WebSocket."""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    logger.info("Call %s answered (SignalWire SID: %s)", call_id, call_sid)

    if call_sid:
        async with get_db_conn() as db:
            await db.execute(
                "UPDATE calls SET provider_call_id=$1, status='in-progress' WHERE id=$2",
                call_sid, call_id,
            )

    # Return <Connect><Stream> XML to start the WebSocket pipeline
    xml = _stream_xml(call_id)
    return _xml(xml)


# ────────────────────────────────────────────────────────────
# SMS — Inbound SMS Callback
# ────────────────────────────────────────────────────────────

@router.post("/sms", operation_id="signalwire_sms_callback")
async def signalwire_sms_callback(request: Request):
    """
    Receive inbound SMS from SignalWire.

    Saves the message to DB, pushes an sms.received event to the
    event mailbox (so agents polling GET /v1/events get notified),
    and dispatches to any registered customer webhooks.
    """
    form = await request.form()
    from_number = form.get("From", "")
    to_number = form.get("To", "")
    text = form.get("Body", "")
    message_sid = form.get("MessageSid", "")
    num_media = int(form.get("NumMedia", "0") or "0")
    media_url = form.get("MediaUrl0", "") if num_media > 0 else ""

    if from_number and not from_number.startswith("+"):
        from_number = f"+{from_number}"
    if to_number and not to_number.startswith("+"):
        to_number = f"+{to_number}"

    logger.info("Inbound SMS from %s to %s: %s", from_number, to_number, text[:80])

    async with get_db_conn() as db:
        number = await db.fetchrow(
            "SELECT * FROM phone_numbers WHERE (phone_number=$1 OR phone_number=$2)",
            to_number, to_number.lstrip("+"),
        )
        if not number:
            return {"status": "unknown_number"}

        # Upsert conversation
        conv = await db.fetchrow(
            "SELECT * FROM conversations WHERE number_id=$1 AND contact_number=$2",
            number["id"], from_number,
        )
        if not conv:
            conv_id = f"conv_{secrets.token_urlsafe(12)}"
            await db.execute(
                """INSERT INTO conversations (id, account_id, agent_id, number_id, contact_number, last_message_at)
                   VALUES ($1,$2,$3,$4,$5,now())""",
                conv_id, number["account_id"], number["agent_id"],
                number["id"], from_number,
            )
        else:
            conv_id = conv["id"]
            await db.execute(
                "UPDATE conversations SET last_message_at = now() WHERE id = $1",
                conv_id,
            )

        # Save inbound message
        msg_id = f"msg_{secrets.token_urlsafe(12)}"
        await db.execute(
            """INSERT INTO messages
               (id, account_id, agent_id, number_id, conversation_id,
                provider_message_id, direction, from_number, to_number, body, media_url)
               VALUES ($1,$2,$3,$4,$5,$6,'inbound',$7,$8,$9,$10)""",
            msg_id, number["account_id"], number["agent_id"],
            number["id"], conv_id, message_sid,
            from_number, to_number, text, media_url or None,
        )

        # ── Push sms.received event to event mailbox ──
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
            logger.info(
                "Inbound SMS %s — pushed sms.received event (from %s)",
                msg_id, from_number,
            )
        except Exception as e:
            logger.error("Failed to push sms.received event for %s: %s", msg_id, e)

    # ── Dispatch to customer webhook (fire-and-forget) ──
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
    except Exception as e:
        logger.warning("Webhook dispatch failed for inbound SMS %s: %s", msg_id, e)

    return _xml("<Response/>")


# ────────────────────────────────────────────────────────────
# Voice — Hangup (StatusCallback)
# ────────────────────────────────────────────────────────────

@router.post("/hangup/{call_id}", operation_id="signalwire_hangup")
async def signalwire_hangup(request: Request, call_id: str):
    """SignalWire POSTs here when the call ends (StatusCallback)."""
    form = await request.form()
    call_status = form.get("CallStatus", "")
    duration = form.get("CallDuration", form.get("Duration", "0"))
    call_sid = form.get("CallSid", "")

    logger.info("Call %s status=%s duration=%ss (SID: %s)", call_id, call_status, duration, call_sid)

    # Only act on terminal states
    if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
        async with get_db_conn() as db:
            call = await db.fetchrow("SELECT * FROM calls WHERE id=$1", call_id)
            # Fallback: look up by provider call SID if call_id doesn't match
            if not call and call_sid:
                call = await db.fetchrow(
                    "SELECT * FROM calls WHERE provider_call_id=$1", call_sid
                )
                if call:
                    call_id = call["id"]
                    logger.info("Hangup: resolved call by CallSid %s → %s", call_sid, call_id)
            if not call:
                logger.warning("Hangup: no call found for id=%s sid=%s", call_id, call_sid)
                return _xml("<Response/>")

            duration_secs = int(duration) if str(duration).isdigit() else 0

            await db.execute(
                """UPDATE calls SET status='completed', duration_seconds=$1, ended_at=now()
                   WHERE id=$2 AND status!='completed'""",
                duration_secs, call_id,
            )

            # ── Billing: charge $0.10/min for the call ──
            if duration_secs > 0 and call.get("account_id"):
                call_cost = calculate_call_cost(duration_secs)
                direction = call.get("direction", "unknown")
                try:
                    await debit_account(
                        db,
                        call["account_id"],
                        call_cost,
                        txn_type="call_charge",
                        reference_id=call_id,
                        description=(
                            f"{direction.capitalize()} call {duration_secs}s "
                            f"({call.get('from_number', '')} → {call.get('to_number', '')})"
                        ),
                    )
                    logger.info(
                        "Call %s — billed $%.4f for %ds (%s)",
                        call_id, call_cost, duration_secs, direction,
                    )
                except ValueError as e:
                    # Insufficient balance — log but don't block call completion
                    logger.warning(
                        "Call %s — billing failed (insufficient balance): %s", call_id, e
                    )

            # ── Push call.completed event with transcript to event mailbox ──
            transcript = _parse_transcript(call.get("transcript"))
            event_id = f"evt_{secrets.token_urlsafe(12)}"
            event_payload = {
                "call_id": call_id,
                "status": call_status,
                "direction": call.get("direction", ""),
                "from_number": call.get("from_number", ""),
                "to_number": call.get("to_number", ""),
                "duration_seconds": duration_secs,
                "transcript": transcript,
            }

            event_type = "call.completed" if call_status == "completed" else f"call.{call_status}"

            try:
                await db.execute(
                    """INSERT INTO event_mailbox
                       (event_id, account_id, agent_id, event_type, payload)
                       VALUES ($1, $2, $3, $4, $5)""",
                    event_id,
                    call.get("account_id"),
                    call.get("agent_id"),
                    event_type,
                    json.dumps(event_payload),
                )
                logger.info(
                    "Call %s — pushed %s event (transcript: %d turns)",
                    call_id, event_type, len(transcript),
                )
            except Exception as e:
                logger.error("Failed to push event for call %s: %s", call_id, e)

    return _xml("<Response/>")


# ────────────────────────────────────────────────────────────
# Voice — Inbound Call on a SignalWire US number
# ────────────────────────────────────────────────────────────

@router.post("/inbound", operation_id="signalwire_inbound_call")
async def signalwire_inbound_call(request: Request):
    """Handle incoming calls on SignalWire US numbers."""
    form = await request.form()
    from_number = form.get("From", "")
    to_number = form.get("To", "")
    call_sid = form.get("CallSid", "")

    if from_number and not from_number.startswith("+"):
        from_number = f"+{from_number}"
    if to_number and not to_number.startswith("+"):
        to_number = f"+{to_number}"

    logger.info("Inbound call (SignalWire): %s -> %s (SID: %s)", from_number, to_number, call_sid)

    async with get_db_conn() as db:
        number = await db.fetchrow(
            "SELECT * FROM phone_numbers WHERE (phone_number=$1 OR phone_number=$2) AND status='active'",
            to_number, to_number.lstrip("+"),
        )
        if not number:
            return _xml("<Response><Say>This number is not configured. Goodbye.</Say></Response>")

        # ── Billing: reject inbound calls if account has insufficient balance ──
        balance = await db.fetchval(
            "SELECT balance FROM accounts WHERE id = $1", number["account_id"]
        )
        if balance is not None and float(balance) < 0.10:
            logger.warning(
                "Inbound call rejected — account %s has insufficient balance ($%.2f)",
                number["account_id"], float(balance),
            )
            return _xml(
                "<Response><Say>This number is temporarily unavailable due to "
                "insufficient account balance. Please contact the account owner. "
                "Goodbye.</Say></Response>"
            )

        agent = await db.fetchrow("SELECT * FROM agents WHERE id=$1", number["agent_id"])

        call_id = f"call_{secrets.token_urlsafe(12)}"
        await db.execute(
            """INSERT INTO calls
               (id, account_id, agent_id, number_id, provider_call_id,
                direction, from_number, to_number, system_prompt, status, started_at)
               VALUES ($1,$2,$3,$4,$5,'inbound',$6,$7,$8,'in-progress',now())""",
            call_id, number["account_id"], number["agent_id"], number["id"],
            call_sid, from_number, to_number,
            agent["system_prompt"] if agent else "",
        )

        # ── Push call.received event to event mailbox ──
        # This is how the real agent (Claude/Hermes) learns about inbound calls
        call_received_payload = {
            "call_id": call_id,
            "agent_id": number["agent_id"],
            "number": to_number,
            "from": from_number,
            "direction": "inbound",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        event_id = f"evt_{secrets.token_urlsafe(12)}"
        try:
            await db.execute(
                """INSERT INTO event_mailbox
                   (event_id, account_id, agent_id, event_type, payload)
                   VALUES ($1, $2, $3, 'call.received', $4)""",
                event_id,
                number["account_id"],
                number["agent_id"],
                json.dumps(call_received_payload),
            )
            logger.info(
                "Inbound call %s — pushed call.received event (from %s)",
                call_id, from_number,
            )
        except Exception as e:
            logger.error("Failed to push call.received event for %s: %s", call_id, e)

    # Set StatusCallback on the live call so we get billed when it ends (fire-and-forget)
    # MUST NOT block — SignalWire is waiting for our XML response.
    if call_sid:
        async def _set_status_callback():
            try:
                hangup_url = f"{settings.base_url_clean}/signalwire/hangup/{call_id}"
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"{_get_base_url()}/Calls/{call_sid}.json",
                        auth=_get_auth(),
                        data={
                            "StatusCallback": hangup_url,
                            "StatusCallbackMethod": "POST",
                        },
                    )
                logger.info("Inbound call %s — set StatusCallback to %s", call_id, hangup_url)
            except Exception as e:
                logger.warning("Failed to set StatusCallback for inbound call %s: %s", call_id, e)

        asyncio.create_task(_set_status_callback())

    # Connect to our streaming pipeline via WebSocket (NOT <Gather>)
    xml = _stream_xml(call_id)
    return _xml(xml)


# ────────────────────────────────────────────────────────────
# Voice — Inbound Call Hangup (number-level StatusCallback fallback)
# ────────────────────────────────────────────────────────────

@router.post("/inbound_hangup", operation_id="signalwire_inbound_hangup")
async def signalwire_inbound_hangup(request: Request):
    """
    Fallback hangup handler for inbound calls on numbers that still have
    the old StatusCallback URL (/signalwire/hangup/inbound_status).
    Looks up the call by CallSid instead of our internal call_id.
    """
    form = await request.form()
    call_status = form.get("CallStatus", "")
    duration = form.get("CallDuration", form.get("Duration", "0"))
    call_sid = form.get("CallSid", "")

    logger.info("Inbound hangup (fallback): status=%s duration=%ss (SID: %s)", call_status, duration, call_sid)

    if not call_sid:
        return _xml("<Response/>")

    if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
        async with get_db_conn() as db:
            call = await db.fetchrow(
                "SELECT * FROM calls WHERE provider_call_id=$1", call_sid
            )
            if not call:
                logger.warning("Inbound hangup: no call found for CallSid %s", call_sid)
                return _xml("<Response/>")

            call_id = call["id"]
            duration_secs = int(duration) if str(duration).isdigit() else 0

            await db.execute(
                """UPDATE calls SET status='completed', duration_seconds=$1, ended_at=now()
                   WHERE id=$2 AND status!='completed'""",
                duration_secs, call_id,
            )

            # ── Billing: charge for the inbound call ──
            if duration_secs > 0 and call.get("account_id"):
                call_cost = calculate_call_cost(duration_secs)
                try:
                    await debit_account(
                        db,
                        call["account_id"],
                        call_cost,
                        txn_type="call_charge",
                        reference_id=call_id,
                        description=(
                            f"Inbound call {duration_secs}s "
                            f"({call.get('from_number', '')} -> {call.get('to_number', '')})"
                        ),
                    )
                    logger.info(
                        "Inbound call %s — billed $%.4f for %ds",
                        call_id, call_cost, duration_secs,
                    )
                except ValueError as e:
                    logger.warning(
                        "Inbound call %s — billing failed: %s", call_id, e
                    )

            # Push event to mailbox
            transcript = _parse_transcript(call.get("transcript"))
            event_id = f"evt_{secrets.token_urlsafe(12)}"
            event_type = "call.completed" if call_status == "completed" else f"call.{call_status}"
            try:
                await db.execute(
                    """INSERT INTO event_mailbox
                       (event_id, account_id, agent_id, event_type, payload)
                       VALUES ($1, $2, $3, $4, $5)""",
                    event_id, call.get("account_id"), call.get("agent_id"),
                    event_type, json.dumps({
                        "call_id": call_id,
                        "status": call_status,
                        "direction": "inbound",
                        "from_number": call.get("from_number", ""),
                        "to_number": call.get("to_number", ""),
                        "duration_seconds": duration_secs,
                        "transcript": transcript,
                    }),
                )
            except Exception as e:
                logger.error("Failed to push event for inbound call %s: %s", call_id, e)

    return _xml("<Response/>")
