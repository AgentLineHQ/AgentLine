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

import asyncpg
import httpx

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from agentline.config import settings
from agentline.database import get_db_conn
from agentline.voice.pipeline import run_pipeline
from agentline.voice.voices import resolve_voice_chain, DEFAULT_VOICE_ID
from agentline.voice.owner_mode import (
    OWNER_MODE_SENTINEL,
    OWNER_MODE_GREETING,
    build_owner_prompt,
    is_owner_number,
)
from agentline.billing import calculate_call_cost, debit_account
from agentline.event_bus import publish_event
from agentline.signalwire_client import _get_auth, _get_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signalwire", tags=["SignalWire Events"])

# Provider call statuses that end a call. Stored verbatim on the calls row so
# DB status and emitted event type always agree ('call.failed' ↔ status='failed').
TERMINAL_STATUSES = ("completed", "failed", "busy", "no-answer", "canceled")


# Owner-mode prompt + helpers live in agentline.voice.owner_mode so they
# can be shared with the outbound call path (routers/calls.py) without a
# router-to-router import. See that module for the full task-mode contract.

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


async def _bill_call_once(db, call, call_id: str, duration_secs: int) -> None:
    """
    Bill a call exactly once, no matter how many terminal callbacks arrive.

    SignalWire retries StatusCallbacks on timeouts, and a call can match both
    the call-level (/hangup/{id}) and number-level (/inbound_hangup) callbacks.
    The ledger-existence check runs inside a transaction with a unique index
    on (account_id, txn_type, reference_id), so a duplicate charge either
    short-circuits here or aborts the transaction on the unique violation.
    """
    if duration_secs <= 0 or not call.get("account_id"):
        return
    try:
        async with db.transaction():
            already_billed = await db.fetchval(
                """SELECT 1 FROM billing_ledger
                   WHERE account_id=$1 AND txn_type='call_charge' AND reference_id=$2""",
                call["account_id"], call_id,
            )
            if already_billed:
                logger.info("Call %s — charge already on ledger, skipping duplicate bill", call_id)
                return
            call_cost = calculate_call_cost(duration_secs)
            direction = call.get("direction", "unknown")
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
    except asyncpg.UniqueViolationError:
        logger.info("Call %s — concurrent billing attempt ignored (already charged)", call_id)
    except ValueError as e:
        # Insufficient balance — log but don't block call completion
        logger.warning("Call %s — billing failed (insufficient balance): %s", call_id, e)


async def _finalize_call(db, call, call_status: str, duration_secs: int) -> None:
    """
    Shared terminal-state processing for both StatusCallback handlers.

    First terminal callback: stores the provider status verbatim, bills once,
    and publishes the call.completed / call.failed / call.owner_task event.
    Duplicate callbacks (retry, or both callback URLs firing): only backfill
    duration_seconds — never bill or publish twice.
    """
    call_id = call["id"]

    if call.get("status") in TERMINAL_STATUSES:
        # Already finalized — a later callback may still carry the true duration
        # (e.g. API hangup marked the call completed before the provider report).
        if duration_secs > 0:
            await db.execute(
                """UPDATE calls SET duration_seconds=$1, ended_at=COALESCE(ended_at, now())
                   WHERE id=$2 AND (duration_seconds IS NULL OR duration_seconds < $1)""",
                duration_secs, call_id,
            )
        return

    await db.execute(
        """UPDATE calls SET status=$1, duration_seconds=$2, ended_at=now()
           WHERE id=$3 AND status NOT IN ('completed','failed','busy','no-answer','canceled')""",
        call_status, duration_secs, call_id,
    )

    await _bill_call_once(db, call, call_id, duration_secs)

    transcript = _parse_transcript(call.get("transcript"))
    is_owner_task = (call.get("system_prompt") or "").startswith(OWNER_MODE_SENTINEL)

    if is_owner_task and call_status == "completed":
        event_type = "call.owner_task"
    else:
        event_type = "call.completed" if call_status == "completed" else f"call.{call_status}"

    await publish_event(
        account_id=call["account_id"],
        agent_id=call.get("agent_id"),
        event_type=event_type,
        payload={
            "call_id": call_id,
            "status": call_status,
            "direction": call.get("direction", ""),
            "from_number": call.get("from_number", ""),
            "to_number": call.get("to_number", ""),
            "duration_seconds": duration_secs,
            "transcript": transcript,
            "is_owner_task": is_owner_task,
        },
    )


def _stream_xml(call_id: str) -> str:
    """
    Generate XML: connect to our WebSocket streaming pipeline.

    Uses <Connect><Stream> for bidirectional audio streaming.
    Audio goes to Deepgram STT (not SignalWire's expensive $0.0675/min STT).
    """
    stream_url = f"{settings.ws_base_url}/signalwire/stream/{call_id}"
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

    # Establish Cartesia while call configuration and Deepgram are loading.
    # This selects no voice and synthesizes no audio; it only removes the
    # WebSocket handshake from the callee's first-response latency.
    from agentline.voice.tts import prewarm_cartesia_connection
    tts_prewarm_task = asyncio.create_task(prewarm_cartesia_connection())

    # ── Prompt & greeting resolution chain ──────────────────────────
    # Priority (highest wins):  per-call override → agent default → hardcoded fallback
    #   system_prompt:    call.system_prompt  →  agent.system_prompt  →  generic fallback
    #   initial_greeting: call.initial_greeting → agent.initial_greeting → generic fallback
    #   voice_id:         call.voice_id → agent.voice_id → account.default_voice_id → DEFAULT_VOICE_ID
    system_prompt = "You are a helpful voice assistant. Keep responses brief and conversational."
    initial_greeting = "Hello, how can I help you today?"
    voice_id = DEFAULT_VOICE_ID
    model_tier = "balanced"
    call_direction = "inbound"          # overridden from call record below
    voicemail_message_text = None       # from agent config
    agent_id = None                     # needed for relay-mode webhook dispatch
    account_id = None                   # needed for relay-mode webhook dispatch
    relay_mode = False                  # True when agent has a webhook configured

    try:
        async with get_db_conn() as db:
            call = await db.fetchrow("SELECT * FROM calls WHERE id=$1", call_id)
            if call:
                agent = await db.fetchrow("SELECT * FROM agents WHERE id=$1", call["agent_id"])

                # Load account for default_voice_id
                account = await db.fetchrow(
                    "SELECT * FROM accounts WHERE id=$1", call["account_id"]
                ) if call.get("account_id") else None

                # Step 1: Agent defaults (override hardcoded fallbacks)
                if agent:
                    system_prompt = agent.get("system_prompt") or system_prompt
                    initial_greeting = agent.get("initial_greeting") or initial_greeting
                    voicemail_message_text = agent.get("voicemail_message")
                    model_tier = agent.get("model_tier") or "balanced"

                # Call direction (inbound / outbound)
                call_direction = call.get("direction", "inbound")

                # IDs for relay-mode webhook dispatch
                agent_id = call.get("agent_id")
                account_id = call.get("account_id")

                # Prefer an active outbound agent WebSocket; use a configured
                # webhook as fallback. The pipeline rechecks this each turn.
                if agent_id and account_id:
                    from agentline.webhook_dispatcher import is_webhook_known_dead
                    from agentline.voice.relay_store import get_relay_transport

                    relay_transport = await get_relay_transport(db, account_id, agent_id)
                    if relay_transport is None:
                        relay_mode = False
                        webhook_status = "no live transport — hosted mode"
                    elif relay_transport == "webhook" and is_webhook_known_dead(agent_id):
                        relay_mode = False
                        webhook_status = "configured but known-dead — hosted mode (no call.utterance)"
                    else:
                        relay_mode = True
                        webhook_status = f"{relay_transport} — relay mode"
                else:
                    webhook_status = "no agent/account — hosted mode"

                logger.info(
                    "Call %s — voice stream start direction=%s relay_mode=%s "
                    "(webhook %s)",
                    call_id,
                    call_direction,
                    relay_mode,
                    webhook_status,
                )

                # Voice resolution chain: per-call → agent → account → default
                voice_id = resolve_voice_chain(
                    per_call_voice=call.get("voice_id"),
                    agent_voice=agent.get("voice_id") if agent else None,
                    account_voice=account.get("default_voice_id") if account else None,
                )

                # Step 2: Per-call overrides (highest priority — set via POST /v1/calls)
                if call.get("system_prompt"):
                    system_prompt = call["system_prompt"]

                if call.get("initial_greeting"):
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
            call_direction=call_direction,
            voicemail_message=voicemail_message_text,
            agent_id=agent_id,
            account_id=account_id,
            relay_mode=relay_mode,
        )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for call %s", call_id)
    except Exception as e:
        logger.error("Pipeline error for call %s: %s", call_id, e)
    finally:
        if not tts_prewarm_task.done():
            tts_prewarm_task.cancel()
        await asyncio.gather(tts_prewarm_task, return_exceptions=True)
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

        # ── Publish sms.received (mailbox + webhook) via the central event bus ──
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
    if call_status in TERMINAL_STATUSES:
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
            await _finalize_call(db, call, call_status, duration_secs)

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

        # ── Owner detection ──────────────────────────────────
        is_owner_call = is_owner_number(agent, from_number)
        if is_owner_call:
            logger.info("Inbound call — OWNER DETECTED (from %s)", from_number)

        call_id = f"call_{secrets.token_urlsafe(12)}"
        await db.execute(
            """INSERT INTO calls
               (id, account_id, agent_id, number_id, provider_call_id,
                direction, from_number, to_number, system_prompt, initial_greeting, status, started_at)
               VALUES ($1,$2,$3,$4,$5,'inbound',$6,$7,$8,$9,'in-progress',now())""",
            call_id, number["account_id"], number["agent_id"], number["id"],
            call_sid, from_number, to_number,
            build_owner_prompt(agent) if is_owner_call else (agent["system_prompt"] if agent else ""),
            OWNER_MODE_GREETING if is_owner_call else (agent.get("initial_greeting") if agent else None),
        )

        # ── Publish call.received (mailbox + background webhook) ──
        # publish_event awaits only the fast mailbox insert; webhook delivery
        # is a background task. A dead agent webhook MUST NOT delay the LaML
        # Stream XML below or SignalWire drops the call (failed / no-answer, 0s).
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
                "is_owner_call": is_owner_call,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

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

    # Answer immediately with <Connect><Stream> — voice path is independent of webhooks.
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

    if call_status in TERMINAL_STATUSES:
        async with get_db_conn() as db:
            call = await db.fetchrow(
                "SELECT * FROM calls WHERE provider_call_id=$1", call_sid
            )
            if not call:
                logger.warning("Inbound hangup: no call found for CallSid %s", call_sid)
                return _xml("<Response/>")

            duration_secs = int(duration) if str(duration).isdigit() else 0
            await _finalize_call(db, call, call_status, duration_secs)

    return _xml("<Response/>")
