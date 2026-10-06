"""
AgentLine — Voice Pipeline (Provider-Agnostic)
Orchestrates the full voice loop: Provider audio → Deepgram STT → LLM → Cartesia TTS → Provider audio.

Supports both SignalWire <Connect><Stream> and Plivo bidirectional WebSocket.

Architecture:
  Provider WS (raw mulaw audio in)
      ↓
  Deepgram (streaming STT)
      ↓ [on utterance end]
  LLM (generate response)
      ↓
  Cartesia (TTS → raw mulaw)
      ↓
  Provider WS (audio back to caller)

Cost savings vs SignalWire <Gather>+<Say>:
  SignalWire STT: $0.0675/min  → Deepgram: $0.006/min  (~90% cheaper)
  SignalWire TTS: $0.003/min   → Cartesia: ~$0.002/min  (comparable)
"""

import asyncio
import json
import base64
import logging
import re
import secrets
import time
from datetime import datetime, timezone

from deepgram import LiveTranscriptionEvents

from agentline.config import settings
from agentline.database import get_db_conn
from agentline.event_bus import publish_event
from agentline.webhook_dispatcher import (
    is_webhook_known_dead,
    mark_webhook_dead,
)
from agentline.voice.stt import create_deepgram_connection, get_stt_options
from agentline.voice.llm import llm_response_stream
from agentline.voice.tts import tts_cartesia, tts_cartesia_stream
from agentline.voice.turn_taking import SemanticTurnDetector
from agentline.voice.voices import (
    resolve_voice_id,
    build_voice_change_prompt,
    is_valid_voice_id,
)
from agentline.voice.dtmf import generate_dtmf
from agentline.voice.ivr import (
    build_outbound_turn_hint,
    is_live_greeting,
    resolve_outbound_action,
)
from agentline.voice.owner_mode import OWNER_MODE_SENTINEL
from agentline.voice.relay_context import (
    extract_context,
    queue_relay_acknowledgement,
    queue_relay_speech,
)
from agentline.voice.relay_store import (
    cancel_relay_turn,
    create_relay_turn,
    deliver_turn_context,
    get_relay_transport,
    wait_for_turn_context,
)

logger = logging.getLogger(__name__)

# Matches [DTMF:1], [DTMF:1ww2], [DTMF:*69#], etc. — the sentinel the LLM emits
# when it hears an IVR menu so the agent can press a real button (see generate_dtmf).
# IGNORECASE + \s* tolerate LLM quirks like [dtmf:2] or [DTMF: 1 ].
_DTMF_RE = re.compile(r"\[DTMF:\s*([0-9*#A-DWw,]+)\s*\]", re.IGNORECASE)


class _CallDtmfState:
    """Per-call DTMF / voicemail flags.

    Nested pipeline functions must mutate *attributes* on this object.
    A bare ``last_dtmf_digits = ...`` inside a nested function (without
    ``nonlocal``) makes the name local for the whole function and raises
    UnboundLocalError on every outbound turn — the LLM succeeds, TTS
    never runs, the agent is silent.
    """

    __slots__ = ("last_digits", "leave_vm_after")

    def __init__(self) -> None:
        self.last_digits: str | None = None
        self.leave_vm_after = False

# Matches [VOICE:female-2], [VOICE:male-1], etc. — the sentinel the LLM emits
# when the caller asks for a different voice mid-call.  The pipeline intercepts
# it (see _speculative_generate / _relay_generate), swaps the active Cartesia
# voice_id, and TTS's the surrounding text in the new voice.
_VOICE_CHANGE_RE = re.compile(r"\[VOICE:\s*([a-zA-Z0-9\-]+)\s*\]", re.IGNORECASE)

# ── Turn-taking tuning ────────────────────────────────────────────
# End-of-turn patience is decided SEMANTICALLY (see turn_taking.py):
# after Deepgram signals an endpoint (a pause), P(caller finished their
# turn) is estimated and the extra wait scales from 1.1s (mid-thought
# fragment, keep the floor) down to 0s (crisp question, answer now).
# The user resuming speech inside the window still cancels everything
# and rolls the turn back — the flow is identical to the old fixed
# debounce, just with adaptive duration.

# Minimum number of audio chunks the agent must play before barge-in
# is honoured.  Prevents the agent from being cut off by echo/noise
# in the first ~100ms of playback.
MIN_CHUNKS_BEFORE_BARGEIN = 3

# Max time the playback loop may block waiting for the next TTS chunk
# before re-checking the barge-in flag.  Bounds how long the agent can
# keep talking over a barging-in human when TTS is momentarily starved.
BARGEIN_POLL_SECONDS = 0.05

# Playback must be at least this old before a SpeechStarted barge-in may
# flush the provider buffer directly (see on_speech_started).  Analogue
# line echo of the agent's own first words can look like caller speech;
# this keeps the instant-clear path from self-interrupting playback start.
MIN_BARGEIN_CLEAR_DELAY = 0.25

# Common filler words/sounds.  If an entire speech_final segment contains
# ONLY these tokens we skip it and keep buffering — the user is thinking,
# not finished.
FILLER_WORDS = {
    "uh", "um", "umm", "uhh", "uh-huh", "uh huh",
    "hmm", "hm", "hmmm",
    "oh", "ohh", "ah", "ahh", "er", "eh",
    "like", "so", "well", "okay", "ok",
    "you know", "i mean", "let me think",
}


def _is_only_filler(text: str) -> bool:
    """Return True if *text* consists entirely of filler words/sounds."""
    words = text.lower().strip().split()
    return len(words) > 0 and all(w in FILLER_WORDS for w in words)


# ── Natural speech & emotion layer ────────────────────────────────
# Prepended to the system prompt on every NORMAL call so the agent
# sounds like a real human, not a scripted bot.  The raw model tends to
# produce stiff, robotic turns on phone calls; this layer adds two
# capabilities it otherwise under-delivers:
#   1. Conversational fillers / disfluencies ("um", "I mean", "you know")
#   2. Emotional attunement — reading the caller's mood and matching it.
# Skipped for owner-mode calls (detected via OWNER_MODE_SENTINEL),
# which are deliberately terse task-capture sessions.

NATURAL_SPEECH = """\
SPEAK LIKE A REAL HUMAN ON A PHONE CALL — not a scripted assistant.

1. FILLER WORDS & DISFLUENCIES (CRITICAL — this is THE thing that makes you sound human):
   You MUST use conversational fillers in ALMOST EVERY response — typically
   2-3 per longer turn, and at least one even in a short reply. A reply with
   ZERO fillers sounds robotic and counts as a failure. Do not be stingy.

   Rotate through these so you don't repeat the same one (variety matters):
   "um", "uh", "hmm", "let me think", "let's see", "I mean", "you know",
   "like", "well", "so", "actually", "right", "okay so", "hang on".

   Place them WHERE a real person naturally would:
   - At the START, before answering:           "Hmm, yeah, so..."
   - Mid-sentence, before the key point:       "the thing is, like, your number
                                                 isn't registered yet."
   - While considering or recalling:           "let me think... okay, I see it."
   - Transitioning between thoughts:           "Right, so the other thing is..."
   - Softening a no or a limitation:           "I mean, I'd love to, but..."

   EXAMPLES — match THIS density (these are your template):
   Caller: "Can you check my balance?"
   You:    "Yeah, let me take a look... hmm, okay, so your balance is $12.50."

   Caller: "Why didn't the call go through?"
   You:    "Right, so, uh, let me think about that. I mean, it could be a
           couple things — do you know if the number was actually valid?"

   Caller: "Hello?"
   You:    "Hey, yeah, hi! So, um, what can I help you with today?"

   Notice the fillers at the start, mid-thought, and while thinking. Every
   one of your replies should feel like a person thinking on their feet.

2. EMOTIONAL ATTUNEMENT (mirror the caller):
   Read the caller's emotional state from their tone and words, then MATCH it:
   - Frustrated / upset   -> empathetic, warm, reassuring. "Oh, I'm really
                             sorry about that — um, let me sort this out
                             for you right now."
   - Happy / excited      -> match their energy and enthusiasm.
   - Confused / hesitant  -> slow down, be patient. "Hmm, okay, so what
                             you're saying is..."
   - Calm / businesslike  -> professional but still warm and natural.
   Express the emotion through word choice, pacing and fillers — never by
   labelling it (do NOT say "you sound upset").

3. NATURAL CADENCE:
   - Use contractions: "I'll", "that's", "can't", "let me", "we've".
   - Speak in fragments and run-ons sometimes — not only perfect sentences.
   - Begin thoughts naturally with "So", "Well", "Okay", "Right", "Actually".
   - Keep replies SHORT: 1-3 sentences, suited to spoken conversation.

EXACT-TOKEN RULE: When another instruction tells you to output an exact
marker (e.g. [DTMF:1] or [VOICEMAIL_DETECTED]), output ONLY that marker —
no fillers, no extra words. Exact-token instructions always override the
natural-speech rules above.
"""

# Short reinforcement appended at the END of the assembled prompt so
# recency bias keeps the voice-style rules in effect even after a long
# user / outbound prompt.  Cheap (few tokens), high leverage.
NATURAL_SPEECH_REMINDER = """\
REMINDER — VOICE STYLE: You're on a live phone call, not writing text. Sound
human: lead MOST replies with a filler ("hmm", "yeah, so", "well", "let me
think", "I mean", "you know"), put another mid-thought when considering
something, mirror the caller's emotion, use contractions, and keep it to 1-3
spoken sentences. A stiff, filler-free reply is a failure. (Exception: when
an exact marker like [DTMF:...] or [VOICEMAIL_DETECTED] is required, output
ONLY that marker.)
"""


# ── Relay-mode context-wait tuning ────────────────────────────────
# Relay calls dispatch immediately. If the response is not instant, inbound
# callers hear one deterministic acknowledgement, never hosted LLM filler.
# The returned relay text is then spoken directly.
#
# Runtime connectors get three minutes for tool work. The server waits longer
# so their final context can still be delivered and consumed.
RELAY_WEBHOOK_HTTP_TIMEOUT = 180.0
RELAY_CONTEXT_POLL_INTERVAL = 8.0
RELAY_MAX_WAIT = 210.0
RELAY_ACKNOWLEDGEMENT_DELAY = 0.75
RELAY_ACKNOWLEDGEMENT = "Let me check that for you."


def _build_relay_instructions(call_id: str, turn_id: str, push_token: str) -> str:
    """Build context_instructions for a call.utterance event.

    One path only: do work fast, then POST context to the live call. The
    push_token ships in the (already-authenticated) webhook payload, so the
    agent does NOT need the account API key to answer — it just echoes the
    token back. This removes the most common failure (wrong/missing key → 401).
    """
    base = settings.base_url_clean
    full_url = f"{base}/v1/calls/{call_id}/context?turn_id={turn_id}"
    return (
        "ACTION REQUIRED: LIVE CALLER WAITING\n"
        "A human is on a LIVE phone call RIGHT NOW waiting for your answer.\n"
        "\n"
        "WARNING — WORK AS FAST AS POSSIBLE:\n"
        "  This turn expires after three minutes. The caller hears one brief "
        "checking acknowledgement, then waits for your response. Slow work "
        "wastes money and frustrates "
        "callers. Complete the task and push your final spoken answer immediately.\n"
        "\n"
        "STEP 1: Do your work (email lookup, tool call, etc.) — FAST\n"
        "STEP 2: Push the exact caller-ready response (REQUIRED — WhatsApp is NOT enough).\n"
        "        Use the push_token from this event — NO API KEY NEEDED:\n"
        f"  curl -X POST {full_url} "
        f"-H \"X-Push-Token: {push_token}\" "
        "-H \"Content-Type: application/json\" "
        "-d '{\"context\": \"YOUR SHORT CALLER-READY ANSWER\"}'\n"
        "\n"
        "(If you have $AGENTLINE_API_KEY, you may send "
        "\"Authorization: Bearer $AGENTLINE_API_KEY\" instead of X-Push-Token.)\n"
        "\n"
        f"TURN ID: {turn_id}. Echo it exactly. Context for another turn is rejected.\n"
        "\n"
        f"Or use MCP tool: push_call_context(call_id=\"{call_id}\", "
        f"body={{turn_id: \"{turn_id}\", context: \"YOUR SHORT CALLER-READY ANSWER\"}})\n"
        "\n"
        "DO NOT:\n"
        "  - Send the answer ONLY to WhatsApp, SMS, or any chat channel\n"
        "  - Return {\"status\": \"accepted\"} without ever pushing context\n"
        "  - Create skills, plans, files, or documentation\n"
        "  - Ask follow-up questions or request clarification\n"
        "  - Delay or deliberate — answer with what you have NOW\n"
        "  - Return notes or facts for another model to rewrite; your text is spoken verbatim\n"
        "\n"
        "The caller CANNOT see text messages. They hear ONLY what you push "
        "to the call via the URL above. Push context NOW."
    )


# ── Outbound call prompt context ──────────────────────────────────
# Prepended to the system prompt on outbound calls so the LLM knows it's
# the caller, not the receiver.  The LLM handles voicemail, IVR, screening,
# and live-answer scenarios through natural language understanding — no
# brittle keyword matching needed.

OUTBOUND_CONTEXT = """\
OUTBOUND CALL CONTEXT — YOU are the one who initiated this call. The other person did NOT call you.

CRITICAL RULE: Do NOT speak first. LISTEN to what the other end says, then respond appropriately:

1. LIVE HUMAN (they say "Hello?", "Hi", "Yeah?", "Who is this?", or similar greeting):
   → Introduce yourself and state your purpose naturally.

2. VOICEMAIL GREETING (you hear "You've reached...", "Leave a message...", "not available",
   "is unavailable", "after the beep", "can't come to the phone", or a mailbox number/name
   followed by "is not available"):
   → This is NOT a phone menu. Do NOT press any keys.
   → Your ENTIRE response must be ONLY: [VOICEMAIL_DETECTED]
   → Example: "12418174 is not available." → [VOICEMAIL_DETECTED]

3. VOICEMAIL KEY MENU (a mailbox says it did not get a message, or asks you to press a
   key to record or disconnect — e.g. "To disconnect, press 1. To record your message, press 2."):
   → Press the key that records / leaves a message (usually 2): [DTMF:2]
   → Do NOT press 0. 0 is not an option unless the menu listed it.
   → If there is no record option, press the disconnect key.

4. IVR / PHONE MENU (you hear "Press 1 for sales", "For support press 2", "Dial 0 for the operator"):
   → Press a key that was ACTUALLY offered. Output ONLY this exact marker —
     no other words, before or after:
         [DTMF:1]          ← press a single offered digit
         [DTMF:1ww2]       ← press 1, pause, press 2 (menus / extensions)
   → Match the option to your call's purpose (stated in your introduction/instructions).
   → Press 0 ONLY if the menu listed 0 (operator). 0 is not a default and is
     usually ignored by voicemail systems.
   → Do NOT speak the number out loud. The system turns [DTMF:...] into a real
     telephone button press automatically.
   → If the menu explicitly asks you to SAY something ("say 'yes'", "speak your name"),
     respond with the spoken word instead — DTMF is only for key presses.
   → After outputting the marker, stay silent and listen for the next prompt.
   → If the SAME menu repeats, your last key was wrong or ignored — press a
     DIFFERENT offered key. Never repeat a key that just failed.
   → Once a human answers, continue as normal.

5. CALL SCREENING (you hear "State your name", "Who is calling?", "Record your name and purpose"):
   → State your name/identity and purpose clearly and briefly.
   → Wait for the person to come on the line, then introduce yourself.

6. PERSON ANSWERED BUT SAID NOTHING (you receive "[The person answered the phone but hasn't said anything yet]"):
   → Introduce yourself naturally, as if you're making a normal phone call.
"""


# ── Fire-and-forget DB write ─────────────────────────────────────

async def _save_transcript(call_id: str, turns: list[dict]):
    """Persist transcript in background — runs off the audio hot path."""
    try:
        async with get_db_conn() as db:
            await db.execute(
                "UPDATE calls SET transcript=$1 WHERE id=$2",
                json.dumps(turns), call_id,
            )
    except Exception as e:
        logger.warning("Failed to save transcript for call %s: %s", call_id, e)


async def _persist_agent_voice(call_id: str, agent_id: str, voice: str):
    """Persist a mid-call voice change to the agent row (fire-and-forget).

    Writes the new voice to ``agents.voice_id`` so subsequent calls pick it up
    via the normal resolution chain (per-call > agent > account > default).
    """
    try:
        async with get_db_conn() as db:
            await db.execute(
                "UPDATE agents SET voice_id=$1 WHERE id=$2",
                voice, agent_id,
            )
        logger.info(
            "Call %s — persisted voice '%s' to agent %s for future calls",
            call_id, voice, agent_id,
        )
    except Exception as e:
        logger.warning(
            "Call %s — failed to persist voice for agent %s: %s",
            call_id, agent_id, e,
        )


def _resolve_and_persist_voice(
    requested: str,
    call_id: str,
    agent_id: str | None,
    is_owner_call: bool,
) -> str | None:
    """Validate a requested voice, persist it (owner calls only), return UUID.

    Returns the resolved Cartesia UUID on success, or None if *requested* is
    not a known preset / valid UUID (the caller keeps its current voice).

    Persistence is gated to owner calls so a random caller can't permanently
    reconfigure the agent's voice for everyone.
    """
    if not is_valid_voice_id(requested):
        logger.warning(
            "Call %s — invalid voice '%s' requested, keeping current voice",
            call_id, requested,
        )
        return None
    resolved = resolve_voice_id(requested)
    persisted = is_owner_call and agent_id
    logger.info(
        "Call %s — voice changed to '%s' (%s)%s",
        call_id, requested, resolved,
        " [persisted to agent]" if persisted else "",
    )
    if persisted:
        asyncio.create_task(_persist_agent_voice(call_id, agent_id, requested))
    return resolved


# ── Provider-specific audio send helpers ──────────────────────────

async def _send_audio_signalwire(ws, audio_bytes: bytes, stream_sid: str):
    """Send audio back to caller via SignalWire <Connect><Stream> WebSocket."""
    if not audio_bytes:
        return
    payload = base64.b64encode(audio_bytes).decode("ascii")
    msg = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {
            "payload": payload,
        },
    }
    await ws.send_json(msg)
    logger.debug("Sent %d bytes audio to SignalWire (streamSid: %s)", len(audio_bytes), stream_sid[:8])


async def _clear_audio_signalwire(ws, stream_sid: str):
    """Tell SignalWire to flush its audio buffer so the caller stops hearing the agent immediately."""
    try:
        await ws.send_json({"event": "clear", "streamSid": stream_sid})
        logger.debug("Sent clear event to SignalWire (streamSid: %s)", stream_sid[:8])
    except Exception as e:
        logger.warning("Failed to send clear event: %s", e)


async def _send_audio_plivo(ws, audio_bytes: bytes, _stream_sid: str = ""):
    """Send audio back to caller via Plivo bidirectional WebSocket."""
    if not audio_bytes:
        return
    payload = base64.b64encode(audio_bytes).decode("ascii")
    await ws.send_json({
        "event": "playAudio",
        "media": {"payload": payload, "contentType": "audio/x-mulaw;rate=8000"},
    })


async def _clear_audio_plivo(ws, _stream_sid: str = ""):
    """Best-effort playback stop on a Plivo stream."""
    try:
        await ws.send_json({"event": "clearAudio"})
    except Exception as e:
        logger.warning("Failed to send Plivo clear event: %s", e)


# Provider send function registry
PROVIDER_SEND = {
    "signalwire": _send_audio_signalwire,
    "twilio": _send_audio_signalwire,
    "telnyx": _send_audio_signalwire,
    "plivo": _send_audio_plivo,
}

# Provider clear function registry. Twilio and Telnyx use the same media JSON as SignalWire.
PROVIDER_CLEAR = {
    "signalwire": _clear_audio_signalwire,
    "twilio": _clear_audio_signalwire,
    "telnyx": _clear_audio_signalwire,
    "plivo": _clear_audio_plivo,
}


async def run_pipeline(
    provider_ws,
    call_id: str,
    system_prompt: str,
    initial_greeting: str | None,
    voice_id: str,
    model_tier: str,
    provider: str = "signalwire",
    call_direction: str = "inbound",
    voicemail_message: str | None = None,
    agent_id: str | None = None,
    account_id: str | None = None,
    relay_mode: bool = False,
):
    """
    Main voice pipeline coroutine. One instance per active call.
    Bridges Provider audio ↔ Deepgram STT ↔ LLM ↔ Cartesia TTS.

    On **inbound** calls the agent greets immediately (current behaviour).
    On **outbound** calls the agent listens first, letting the LLM classify
    the other end (live human / voicemail / IVR / screening) and respond
    appropriately.

    Args:
        provider_ws: WebSocket connection to the telephony provider
        call_id: Internal call ID
        system_prompt: System prompt for the LLM
        initial_greeting: Greeting spoken at inbound start or after an outbound
                          callee's first live greeting
        voice_id: Cartesia voice ID (UUID or preset name — resolved before use)
        model_tier: LLM model tier (turbo/balanced/max)
        provider: 'signalwire' or 'plivo'
        call_direction: 'inbound' or 'outbound' — controls greeting behaviour
        voicemail_message: Message to leave if outbound call reaches voicemail
        agent_id: Agent ID (needed for relay-mode webhook dispatch)
        account_id: Account ID (needed for relay-mode webhook dispatch)
        relay_mode: Legacy discovery hint. A currently usable relay transport
                    is detected per turn; inbound turns are relay-authoritative.
                    Outbound greeting, IVR, and voicemail controls remain local.
    """
    voice_id = resolve_voice_id(voice_id)
    send_audio = PROVIDER_SEND.get(provider, _send_audio_signalwire)
    clear_audio = PROVIDER_CLEAR.get(provider, _clear_audio_signalwire)

    # Owner calls (the owner talking to their own agent) get their mid-call
    # voice changes persisted to the agent row so the choice sticks for future
    # calls.  Detected via the OWNER MODE sentinel prepended by
    # build_owner_prompt(); random callers' voice changes stay session-only.
    is_owner_call = (system_prompt or "").lstrip().startswith(OWNER_MODE_SENTINEL)

    # ── Natural speech & emotion layer ───────────────────────────────
    # Prepend humanity guidance (fillers + emotional mirroring) to EVERY
    # call so the agent sounds like a real person — including owner-mode
    # task sessions.  This prepend is in-memory only; the OWNER MODE
    # sentinel stored in the DB prompt is left untouched, so hangup
    # handlers still detect owner calls and emit call.owner_task.
    system_prompt = NATURAL_SPEECH + "\n" + (system_prompt or "")

    conversation_history: list[dict] = []
    transcript_turns: list[dict] = []
    stream_sid = ""  # Set when we receive the 'start' event with metadata
    greeting_sent = False
    outbound_introduction_sent = False
    pending_response_task: asyncio.Task | None = None  # debounce handle

    # Barge-in signal: set when the user starts speaking while agent is playing.
    # Checked between audio chunks — no latency overhead, just an Event.is_set() check.
    barge_in = asyncio.Event()
    agent_speaking = False  # True while we're actively flushing audio to the caller
    agent_speaking_since = 0.0  # monotonic timestamp of current playback start

    # ── Outbound call state ───────────────────────────────────────
    first_speech_received = asyncio.Event()   # Set when callee speaks for the first time
    voicemail_detected = asyncio.Event()      # Set when LLM outputs [VOICEMAIL_DETECTED]
    voicemail_greeting_ended = asyncio.Event() # Set by UtteranceEnd after voicemail detected (beep)
    dtmf = _CallDtmfState()

    # Augment system prompt for outbound calls so the LLM knows
    # to listen first and handle voicemail / IVR / screening.
    if call_direction == "outbound":
        outbound_prompt = OUTBOUND_CONTEXT
        if initial_greeting:
            outbound_prompt += (
                f'\nYour configured introduction when a live human answers is: '
                f'"{initial_greeting}"\n'
                f'Use this as the basis for your introduction, adapting it naturally.\n'
            )
        system_prompt = outbound_prompt + "\n" + (system_prompt or "")
        logger.info("Call %s — outbound mode: system prompt augmented with listener-first context", call_id)

    # Reinforce voice style at the END of the prompt — recency bias makes
    # the model far more likely to actually apply the fillers + emotion
    # rules, even after a long user/outbound prompt.
    system_prompt = (system_prompt or "") + "\n" + NATURAL_SPEECH_REMINDER

    # ── Mid-call voice switching capability ───────────────────────
    # Teaches the LLM the [VOICE:preset] marker so a caller can ask the agent
    # to switch voices and the pipeline intercepts it (see _VOICE_CHANGE_RE).
    # We pass the resolved current voice so the LLM knows which preset it's on
    # and can pick a DIFFERENT one for within-gender ("sound like a wiser guy")
    # requests instead of re-emitting its current preset.
    system_prompt += "\n" + build_voice_change_prompt(current_voice_id=voice_id)

    # Set up Deepgram streaming STT
    dg_connection = create_deepgram_connection()
    utterance_buffer: list[str] = []

    def _parse_control_markers(sentence: str) -> tuple[str | None, bool]:
        match = _DTMF_RE.search(sentence)
        return (match.group(1) if match else None, "[VOICEMAIL_DETECTED]" in sentence)

    async def _enqueue_outbound_action(action, audio_queue: asyncio.Queue) -> bool:
        """Play DTMF / flag voicemail. True = this turn is consumed."""
        if action.kind == "passthrough":
            return False
        if action.kind == "drop":
            logger.info(
                "Call %s — dropped outbound control marker (%s)",
                call_id, action.reason,
            )
            return True
        if action.kind == "dtmf" and action.digits:
            logger.info(
                "Call %s — sending DTMF %r (%s)",
                call_id, action.digits, action.reason,
            )
            try:
                dtmf_audio = generate_dtmf(action.digits)
                await audio_queue.put(
                    (dtmf_audio, f"[Agent pressed DTMF: {action.digits}]")
                )
                dtmf.last_digits = action.digits
            except Exception as e:
                logger.error(
                    "Call %s — DTMF generation failed for %r: %s",
                    call_id, action.digits, e,
                )
            if action.then_voicemail:
                voicemail_detected.set()
                dtmf.leave_vm_after = True
            return True
        if action.kind == "voicemail":
            logger.info("Call %s — voicemail action (%s)", call_id, action.reason)
            voicemail_detected.set()
            return True
        return False

    async def _maybe_force_outbound_action(
        utterance: str, audio_queue: asyncio.Queue
    ) -> bool:
        """Skip the LLM for mailbox greetings / record-key menus."""
        if call_direction != "outbound":
            return False
        action = resolve_outbound_action(
            utterance,
            llm_digits=None,
            llm_voicemail=False,
            last_dtmf=dtmf.last_digits,
            has_voicemail_message=bool(voicemail_message),
        )
        if action.kind == "passthrough":
            return False
        logger.info(
            "Call %s — outbound policy bypassed LLM: %s digits=%r (%s)",
            call_id, action.kind, action.digits, action.reason,
        )
        return await _enqueue_outbound_action(action, audio_queue)

    async def _handle_dtmf_voicemail(
        sentence: str, audio_queue: asyncio.Queue, utterance: str
    ) -> bool:
        """Intercept [DTMF:…] / [VOICEMAIL_DETECTED], with outbound corrections."""
        llm_digits, llm_vm = _parse_control_markers(sentence)
        if call_direction == "outbound":
            action = resolve_outbound_action(
                utterance,
                llm_digits=llm_digits,
                llm_voicemail=llm_vm,
                last_dtmf=dtmf.last_digits,
                has_voicemail_message=bool(voicemail_message),
            )
            if action.kind != "passthrough" and (
                llm_digits != action.digits
                or llm_vm != (action.kind == "voicemail")
                or action.then_voicemail
            ):
                logger.info(
                    "Call %s — outbound action: llm_digits=%r llm_vm=%s "
                    "→ %s digits=%r then_vm=%s (%s)",
                    call_id, llm_digits, llm_vm, action.kind,
                    action.digits, action.then_voicemail, action.reason,
                )
            return await _enqueue_outbound_action(action, audio_queue)

        if llm_vm:
            logger.info(
                "Call %s — LLM detected voicemail (sentence: %s)",
                call_id, sentence[:80],
            )
            voicemail_detected.set()
            return True
        if llm_digits:
            logger.info("Call %s — LLM requested DTMF press: %s", call_id, llm_digits)
            try:
                dtmf_audio = generate_dtmf(llm_digits)
                await audio_queue.put(
                    (dtmf_audio, f"[Agent pressed DTMF: {llm_digits}]")
                )
                dtmf.last_digits = llm_digits
            except Exception as e:
                logger.error(
                    "Call %s — DTMF generation failed for %r: %s",
                    call_id, llm_digits, e,
                )
            return True
        return False

    # Semantic turn-taking: decides how much patience each endpoint needs
    # based on P(caller finished their turn). Kept per-call because it
    # tracks the rolling utterance text across finalized STT segments.
    turn_detector = SemanticTurnDetector(
        history_fn=lambda: conversation_history[-6:],
    )

    # ── Speculative execution helpers ─────────────────────────────
    async def _speculative_generate(
        utterance: str,
        audio_queue: asyncio.Queue,
    ):
        """Stream LLM → TTS, buffering audio into *audio_queue*.

        Uses WebSocket streaming TTS so audio chunks arrive as they're
        synthesized.  Each chunk is queued individually for near-instant
        playback once the debounce expires.  A ``None`` sentinel is put
        into the queue when generation finishes (or on error).
        """
        nonlocal voice_id
        try:
            if await _maybe_force_outbound_action(utterance, audio_queue):
                return

            brain = llm_response_stream(
                system_prompt, conversation_history, model_tier
            )

            async for sentence in brain:
                # ── DTMF / voicemail interception ───────────────────
                # Markers become real tones or the leave-message flow.
                # Outbound policy also corrects a bad key (e.g. pressing
                # 0 on a menu that only offered 1 and 2).
                if await _handle_dtmf_voicemail(sentence, audio_queue, utterance):
                    return

                # ── Mid-call voice change ──────────────────────────
                # The LLM emits [VOICE:preset-name] when the caller asks for a
                # different voice.  Strip the marker, swap the active voice_id
                # (visible to every subsequent TTS call this turn), and TTS the
                # remaining text in the new voice.
                voice_match = _VOICE_CHANGE_RE.search(sentence)
                if voice_match:
                    requested = voice_match.group(1)
                    cleaned = _VOICE_CHANGE_RE.sub("", sentence).strip()
                    resolved = _resolve_and_persist_voice(
                        requested, call_id, agent_id, is_owner_call,
                    )
                    if resolved:
                        voice_id = resolved
                    if cleaned:
                        try:
                            async for audio_chunk in tts_cartesia_stream(cleaned, voice_id):
                                await audio_queue.put((audio_chunk, cleaned))
                        except Exception as e:
                            logger.error(
                                "Call %s — TTS failed during voice change: %s",
                                call_id, e,
                            )
                    continue  # skip normal TTS, voice already swapped

                try:
                    async for audio_chunk in tts_cartesia_stream(sentence, voice_id):
                        await audio_queue.put((audio_chunk, sentence))
                except Exception as e:
                    logger.error("Call %s — TTS failed during speculative gen: %s", call_id, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Call %s — speculative generation error: %s", call_id, e)
        finally:
            await audio_queue.put(None)  # sentinel — generation complete

    async def _outbound_introduction_generate(audio_queue: asyncio.Queue):
        """Stream the configured introduction after a live human answers."""
        try:
            async for audio_chunk in tts_cartesia_stream(initial_greeting, voice_id):
                await audio_queue.put((audio_chunk, initial_greeting))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Call %s — outbound introduction TTS failed: %s", call_id, e)
        finally:
            await audio_queue.put(None)

    async def _relay_generate(
        utterance: str,
        audio_queue: asyncio.Queue,
        transport: str,
    ):
        """Relay-mode generation: dispatch immediately and speak its result.

        Flow:
        1. Fire ``call.utterance`` to the agent's webhook.
        2. If needed, play one fixed acknowledgement while context is pushed via
           POST /v1/calls/{call_id}/context (or webhook body),
           the caller barges in, or the call ends.
        3. Speak the relay's caller-ready text directly and without rewriting.

        The audio_queue receives ``(audio_bytes, sentence)`` tuples, followed
        by ``None`` when generation is complete — same contract as
        _speculative_generate, so the flush loop in _schedule_response works
        unchanged.
        """
        nonlocal voice_id
        webhook_task: asyncio.Task | None = None
        webhook_feeder: asyncio.Task | None = None
        acknowledgement_task: asyncio.Task | None = None
        context_ready = asyncio.Event()
        turn_id = f"turn_{secrets.token_urlsafe(12)}"

        async def _stop_delivery_tasks() -> None:
            tasks = [
                task for task in (
                    acknowledgement_task,
                    webhook_feeder,
                    webhook_task,
                )
                if task is not None
            ]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        async def _queue_acknowledgement() -> None:
            try:
                await queue_relay_acknowledgement(
                    RELAY_ACKNOWLEDGEMENT,
                    voice_id,
                    audio_queue,
                    tts_cartesia_stream,
                    context_ready,
                    delay=RELAY_ACKNOWLEDGEMENT_DELAY,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "Call %s — relay acknowledgement TTS failed: %s", call_id, e
                )

        try:
            if await _maybe_force_outbound_action(utterance, audio_queue):
                return

            # The transport may have failed after the per-turn health check.
            if transport == "webhook" and is_webhook_known_dead(agent_id):
                logger.info(
                    "Call %s — relay skipped (webhook known-dead); "
                    "hosted answer for '%s'",
                    call_id, utterance[:60],
                )
                await _speculative_generate(utterance, audio_queue)
                return

            async with get_db_conn() as db:
                push_token = await create_relay_turn(
                    db,
                    call_id=call_id,
                    turn_id=turn_id,
                    account_id=account_id,
                    agent_id=agent_id,
                )

            event_payload = {
                "call_id": call_id,
                "turn_id": turn_id,
                "session_key": f"agentline:{agent_id}:{call_id}",
                "utterance": utterance,
                "conversation": list(conversation_history),
                "turn_index": len(transcript_turns),
                "respond_via": "push_context",
                "response_mode": "direct_speech",
                "push_context_url": (
                    f"{settings.base_url_clean}/v1/calls/{call_id}/context"
                    f"?turn_id={turn_id}"
                ),
                "push_context_method": "POST",
                "push_token": push_token,
                "api_base": settings.base_url_clean,
                "action_required": "PUSH_CONTEXT_NOW",
                "action_urgency": "ASAP_LIVE_CALLER",
                "action_speed_warning": (
                    "Work as FAST as possible. The caller is on hold. "
                    "This turn expires after three minutes; every second counts."
                ),
                "context_instructions": _build_relay_instructions(
                    call_id, turn_id, push_token
                ),
            }

            async def _feed_webhook_context():
                try:
                    result = await webhook_task
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        "Call %s — relay: webhook feeder error: %s", call_id, e,
                    )
                    mark_webhook_dead(agent_id)
                    return

                # Hard delivery failure was recorded by the dispatcher.
                if is_webhook_known_dead(agent_id):
                    logger.warning(
                        "Call %s — relay: webhook hard-failed mid-turn — "
                        "disabling call.utterance for rest of call",
                        call_id,
                    )
                    return

                ctx = extract_context(result)
                if ctx:
                    logger.info(
                        "Call %s — relay: context via webhook response (%d chars)",
                        call_id, len(ctx),
                    )
                    async with get_db_conn() as db:
                        await deliver_turn_context(
                            db,
                            call_id=call_id,
                            turn_id=turn_id,
                            context=ctx,
                            account_id=account_id,
                        )
                else:
                    logger.info(
                        "Call %s — relay: webhook response had no context "
                        "(agent must POST /v1/calls/%s/context)",
                        call_id, call_id,
                    )

            # Dispatch before doing any hosted generation. A connected relay
            # is authoritative for every inbound utterance.
            webhook_start = time.monotonic()
            webhook_task = asyncio.create_task(
                publish_event(
                    account_id=account_id,
                    agent_id=agent_id,
                    event_type="call.utterance",
                    payload=event_payload,
                    await_webhook_response=transport == "webhook",
                    webhook_timeout=RELAY_WEBHOOK_HTTP_TIMEOUT,
                    deliver_webhook=transport == "webhook",
                    persist_mailbox=transport == "websocket",
                    require_mailbox=transport == "websocket",
                )
            )
            if call_direction == "inbound":
                acknowledgement_task = asyncio.create_task(
                    _queue_acknowledgement()
                )
            if transport == "webhook":
                webhook_feeder = asyncio.create_task(_feed_webhook_context())
            logger.info(
                "Call %s — relay: fired call.utterance turn=%s via %s for '%s'",
                call_id, turn_id, transport, utterance[:60],
            )

            # Wait for context (webhook, WebSocket, or HTTP push).
            # Context arrives through ONE channel fed by BOTH the webhook
            # response (via the feeder above) and the push endpoint
            # (POST /v1/calls/{call_id}/context).  We wait up to
            # RELAY_MAX_WAIT (hard cap) for context to arrive.
            # Without a cap, a closed WebSocket + no context leaves the turn
            # waiting forever. Polling must never enqueue status speech.

            if transport == "websocket" and webhook_task:
                # Ensure the durable mailbox insert completed before waiting.
                # Otherwise task scheduling could leave a caller waiting for
                # an event that was never committed.
                try:
                    await webhook_task
                except Exception as e:
                    logger.warning(
                        "Call %s — could not persist live turn %s: %s",
                        call_id, turn_id, e,
                    )
                    await cancel_relay_turn(call_id, turn_id)
                    return

            context = None
            poll_count = 0
            while True:
                context, turn_terminal = await wait_for_turn_context(
                    call_id,
                    turn_id,
                    timeout=RELAY_CONTEXT_POLL_INTERVAL,
                )
                if context:
                    context_ready.set()
                    if acknowledgement_task and not acknowledgement_task.done():
                        acknowledgement_task.cancel()
                        await asyncio.gather(
                            acknowledgement_task, return_exceptions=True
                        )
                    waited = time.monotonic() - webhook_start
                    logger.info(
                        "Call %s — relay: context received for turn %s (%d chars) "
                        "after %.1fs, generating real answer",
                        call_id, turn_id, len(str(context)), waited,
                    )
                    break
                if turn_terminal:
                    context_ready.set()
                    logger.info(
                        "Call %s — relay: turn %s ended while waiting for context",
                        call_id, turn_id,
                    )
                    return
                waited = time.monotonic() - webhook_start
                if transport == "webhook" and is_webhook_known_dead(agent_id):
                    context_ready.set()
                    logger.warning(
                        "Call %s — relay webhook failed while waiting",
                        call_id,
                    )
                    await cancel_relay_turn(call_id, turn_id)
                    break
                if waited >= RELAY_MAX_WAIT:
                    context_ready.set()
                    logger.warning(
                        "Call %s — relay: TIMEOUT after %.0fs — giving up",
                        call_id, waited,
                    )
                    await cancel_relay_turn(call_id, turn_id)
                    break
                poll_count += 1
                if poll_count == 1 or poll_count % 5 == 0:
                    logger.info(
                        "Call %s — relay: still waiting for turn %s "
                        "(%.0fs elapsed, max %.0fs)",
                        call_id, turn_id, waited, RELAY_MAX_WAIT,
                    )

            # Speak the external agent response directly. Queue each chunk as
            # soon as TTS yields it instead of buffering the complete answer.
            # The playback commit records these exact sentences as the
            # assistant turn, preserving them in conversation history.
            if context:
                try:
                    chunks_queued = await queue_relay_speech(
                        str(context), voice_id, audio_queue, tts_cartesia_stream
                    )
                    if not chunks_queued:
                        raise RuntimeError("TTS returned no audio")
                    logger.info(
                        "Call %s — relay: speaking external response directly "
                        "for turn %s (%d chars)",
                        call_id, turn_id, len(str(context)),
                    )
                    return
                except Exception as e:
                    logger.error(
                        "Call %s — direct relay TTS failed for turn %s: %s",
                        call_id, turn_id, e,
                    )
            else:
                logger.warning("Call %s — relay: no context before deadline", call_id)

        except asyncio.CancelledError:
            # Pipeline cancelled (user barged in or call ended)
            await _stop_delivery_tasks()
            await cancel_relay_turn(call_id, turn_id)
            raise
        except Exception as e:
            logger.error("Call %s — relay generation error: %s", call_id, e)
            await _stop_delivery_tasks()
            await cancel_relay_turn(call_id, turn_id)
        finally:
            # Cancel any still-pending held-open webhook request. Needed when
            # context arrived via the push endpoint (Path B) while the webhook
            # body was still open: the function returns normally (not via
            # CancelledError), so without this the httpx request (timeout=None)
            # would leak as an orphan task until the agent responds.
            await _stop_delivery_tasks()
            await audio_queue.put(None)  # sentinel — generation complete

    async def _schedule_response(utterance: str):
        """Speculative execution: generate response DURING debounce, flush AFTER.

        Instead of wasting the debounce window doing nothing, we:
        1. Immediately start LLM → TTS generation (audio buffered in a queue).
        2. Sleep for DEBOUNCE_SECONDS in parallel.
        3. If the user resumes speaking, cancel everything and roll back.
        4. If the debounce expires, commit the turn and flush the pre-generated
           audio — near-instant playback.

        Result: the user still gets the full debounce patience (no interruptions),
        but perceives almost zero processing delay after the pause.
        """
        nonlocal pending_response_task, outbound_introduction_sent

        audio_queue: asyncio.Queue = asyncio.Queue()
        committed = False  # tracks whether we've committed the turn
        reply_parts: list[str] = []  # sentences actually played (barge-in safety)

        # Tentatively add user message so the LLM has context.
        # On outbound IVR/voicemail turns, append a short [SYSTEM] hint
        # naming the offered keys so the model does not default to 0.
        history_text = utterance
        if call_direction == "outbound":
            hint = build_outbound_turn_hint(
                utterance, dtmf.last_digits, bool(voicemail_message)
            )
            if hint:
                history_text = f"{utterance}\n{hint}"
        conversation_history.append({"role": "user", "content": history_text})

        # A greeting-only first utterance is strong evidence that a human
        # answered. Speak the configured introduction verbatim instead of
        # waiting for an LLM to regenerate it. Ambiguous speech, IVRs, and
        # voicemail continue through the normal classifier/generation path.
        use_outbound_introduction = bool(
            call_direction == "outbound"
            and initial_greeting
            and not outbound_introduction_sent
            and is_live_greeting(utterance)
        )
        if use_outbound_introduction:
            logger.info(
                "Call %s — live outbound greeting detected; "
                "streaming configured introduction without LLM",
                call_id,
            )

        # Re-evaluate the transport each turn so an outbound agent WebSocket
        # can reconnect during a call. WebSocket is preferred over webhook.
        transport = None
        if not use_outbound_introduction and account_id and agent_id:
            try:
                async with get_db_conn() as db:
                    transport = await get_relay_transport(db, account_id, agent_id)
            except Exception as e:
                logger.warning("Call %s — relay transport check failed: %s", call_id, e)
        if transport == "webhook" and is_webhook_known_dead(agent_id):
            transport = None
        if use_outbound_introduction:
            gen_task = asyncio.create_task(
                _outbound_introduction_generate(audio_queue)
            )
        elif transport:
            gen_task = asyncio.create_task(
                _relay_generate(utterance, audio_queue, transport)
            )
        else:
            gen_task = asyncio.create_task(
                _speculative_generate(utterance, audio_queue)
            )

        try:
            # ── Phase 1: Semantic end-of-turn decision ─────────
            # A short silence only means the caller paused. Instead of a
            # flat 0.9s debounce, estimate P(turn finished): crisp
            # questions commit instantly (response audio is
            # already pre-generated speculatively), mid-thought fragments
            # keep the floor for up to 1.1s. Resumed speech still cancels
            # and rolls the turn back — same flow, adaptive duration.
            extra_wait = (
                0.0
                if use_outbound_introduction
                else await turn_detector.decide_wait(utterance)
            )
            if extra_wait > 0:
                await asyncio.sleep(extra_wait)

            # User stayed silent → commit the human turn
            committed = True
            turn_detector.reset()
            logger.info("Call %s — Human: %s", call_id, utterance)
            transcript_turns.append({
                "role": "human",
                "text": utterance,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            asyncio.create_task(_save_transcript(call_id, list(transcript_turns)))

            # ── Phase 2: Flush buffered audio (with barge-in check) ─
            nonlocal agent_speaking, agent_speaking_since
            # Waiting for an LLM, relay, or TTS is silence, not playback. Only
            # expose barge-in after an audio chunk is actually available.
            agent_speaking = False
            agent_speaking_since = 0.0
            barge_in.clear()  # reset from any previous turn
            last_sentence = None
            barged = False
            chunks_sent = 0
            while True:
                # Check barge-in between every chunk — near-zero cost
                if chunks_sent >= MIN_CHUNKS_BEFORE_BARGEIN and barge_in.is_set():
                    logger.info("Call %s — barge-in detected, stopping playback", call_id)
                    barged = True
                    break

                # Wait for the next TTS chunk, but never block longer than
                # BARGEIN_POLL_SECONDS: while TTS is starved (slow LLM/API
                # turn) the queue sits empty, and a plain ``await get()``
                # would park here without ever re-checking the barge-in flag.
                try:
                    item = await asyncio.wait_for(
                        audio_queue.get(), timeout=BARGEIN_POLL_SECONDS
                    )
                except asyncio.TimeoutError:
                    continue
                if item is None:  # sentinel — generation done
                    break
                audio, sentence = item
                if not agent_speaking:
                    agent_speaking = True
                    agent_speaking_since = time.monotonic()
                    barge_in.clear()
                # Only record each sentence text once (multiple chunks per sentence)
                if sentence != last_sentence:
                    reply_parts.append(sentence)
                    last_sentence = sentence
                    logger.debug("Call %s — flushing sentence: %s", call_id, sentence[:80])
                try:
                    await send_audio(provider_ws, audio, stream_sid)
                    chunks_sent += 1
                    if use_outbound_introduction:
                        outbound_introduction_sent = True
                except Exception as e:
                    logger.error("Call %s — send audio failed: %s", call_id, e)

            agent_speaking = False
            agent_speaking_since = 0.0

            # ── Voicemail handling (outbound calls) ───────────────
            if voicemail_detected.is_set():
                gen_task.cancel()
                try:
                    await gen_task
                except (asyncio.CancelledError, Exception):
                    pass

                if voicemail_message:
                    # Classic mailbox: wait for the greeting to finish
                    # (UtteranceEnd ≈ the beep). After a record-key press
                    # the menu already ended — just wait for the beep.
                    if dtmf.leave_vm_after:
                        logger.info(
                            "Call %s — record key sent, waiting for beep...",
                            call_id,
                        )
                        await asyncio.sleep(1.2)
                    else:
                        logger.info(
                            "Call %s — waiting for voicemail greeting to end...",
                            call_id,
                        )
                        if not voicemail_greeting_ended.is_set():
                            try:
                                await asyncio.wait_for(
                                    voicemail_greeting_ended.wait(), timeout=15.0
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "Call %s — voicemail greeting timeout, "
                                    "leaving message now",
                                    call_id,
                                )
                        await asyncio.sleep(0.5)

                    # Leave the voicemail message
                    try:
                        vm_audio = await tts_cartesia(voicemail_message, voice_id)
                        await send_audio(provider_ws, vm_audio, stream_sid)
                        transcript_turns.append({
                            "role": "agent",
                            "text": f"[Voicemail] {voicemail_message}",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        asyncio.create_task(
                            _save_transcript(call_id, list(transcript_turns))
                        )
                        logger.info("Call %s — voicemail message left", call_id)
                    except Exception as e:
                        logger.error(
                            "Call %s — failed to leave voicemail: %s", call_id, e
                        )

                    # Let the audio finish playing before hangup
                    await asyncio.sleep(2.0)
                else:
                    logger.info(
                        "Call %s — voicemail detected, no message configured, hanging up",
                        call_id,
                    )

                # Hang up the call
                await _hangup_outbound_call()
                return

            if barged:
                # Stop LLM+TTS generation and flush the provider's audio buffer
                gen_task.cancel()
                try:
                    await gen_task
                except (asyncio.CancelledError, Exception):
                    pass
                await clear_audio(provider_ws, stream_sid)

                # Commit the partial reply so far (what the user actually heard)
                partial_reply = " ".join(reply_parts)
                if partial_reply:
                    conversation_history.append({"role": "assistant", "content": partial_reply})
                    transcript_turns.append({
                        "role": "agent",
                        "text": partial_reply + " [interrupted]",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    asyncio.create_task(_save_transcript(call_id, list(transcript_turns)))
                logger.info("Call %s — Agent (interrupted): %s", call_id, partial_reply[:100] if partial_reply else "<none>")
                return  # exit — on_transcript will handle the new user speech

            await gen_task  # ensure clean completion

            # ── Phase 3: Commit assistant reply ───────────────────
            full_reply = " ".join(reply_parts)
            conversation_history.append({"role": "assistant", "content": full_reply})
            logger.info("Call %s — Agent: %s", call_id, full_reply)

            transcript_turns.append({
                "role": "agent",
                "text": full_reply,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            asyncio.create_task(_save_transcript(call_id, list(transcript_turns)))

        except asyncio.CancelledError:
            # User resumed speaking during debounce — discard speculative work
            agent_speaking = False
            agent_speaking_since = 0.0
            gen_task.cancel()
            try:
                await gen_task
            except (asyncio.CancelledError, Exception):
                pass
            if committed and reply_parts:
                # Cancelled mid-playback (e.g. the caller's is_final beat the
                # flush loop's barge-in check): commit what was actually
                # spoken so the LLM's context matches what the caller heard.
                partial_reply = " ".join(reply_parts)
                conversation_history.append({"role": "assistant", "content": partial_reply})
                transcript_turns.append({
                    "role": "agent",
                    "text": partial_reply + " [interrupted]",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                asyncio.create_task(_save_transcript(call_id, list(transcript_turns)))
                logger.info(
                    "Call %s — Agent (interrupted): %s",
                    call_id, partial_reply[:100],
                )
            # Roll back the tentative user message if we haven't committed yet
            if not committed:
                for i in range(len(conversation_history) - 1, -1, -1):
                    if conversation_history[i] == {"role": "user", "content": history_text}:
                        conversation_history.pop(i)
                        break
                # Keep the caller's earlier fragment in play: their resumed
                # speech is a continuation, not a replacement. Re-seed the
                # buffer (and the turn detector) so the next endpoint joins
                # old + new words into one utterance for the LLM.
                if utterance:
                    utterance_buffer.insert(0, utterance)
                    turn_detector.reseed(utterance)
            logger.debug("Call %s — speculative response discarded (user resumed speaking)", call_id)
            raise

        finally:
            # Only clear the slot if this task is still the tracked one.
            # A cancelled task's finally can unwind AFTER on_transcript has
            # already installed a newer task (cancel() doesn't yield), and
            # nulling then would orphan the new task — stale responses would
            # become uncancellable and outlive the call.
            if pending_response_task is asyncio.current_task():
                pending_response_task = None

    # ── Outbound call helpers ─────────────────────────────────────

    async def _outbound_silence_fallback():
        """If callee says nothing within 2s, prompt the agent to initiate."""
        nonlocal pending_response_task
        await asyncio.sleep(2.0)
        if first_speech_received.is_set() or voicemail_detected.is_set():
            return  # Speech arrived (or voicemail detected) during the wait

        logger.info("Call %s — outbound: no speech after 2s, agent initiating", call_id)
        first_speech_received.set()  # Prevent re-triggering

        silence_utterance = (
            "[The person answered the phone but hasn't said anything yet]"
        )
        if pending_response_task and not pending_response_task.done():
            pending_response_task.cancel()
        pending_response_task = asyncio.create_task(
            _schedule_response(silence_utterance)
        )

    async def _hangup_outbound_call():
        """Terminate the outbound call via the provider REST API."""
        try:
            async with get_db_conn() as db:
                call = await db.fetchrow(
                    "SELECT provider_call_id FROM calls WHERE id=$1", call_id
                )
                if call and call["provider_call_id"]:
                    from agentline.signalwire_client import hangup_call
                    await hangup_call(call["provider_call_id"])
                    logger.info("Call %s — outbound hangup executed", call_id)
        except Exception as e:
            logger.warning("Call %s — outbound hangup failed: %s", call_id, e)

    # This event fires for each transcript segment
    async def on_transcript(self, result, **kwargs):
        nonlocal pending_response_task

        # Voicemail already detected — ignore further transcripts
        if voicemail_detected.is_set():
            return

        # We only care about finalized transcript segments
        if not result.is_final:
            return

        sentence = result.channel.alternatives[0].transcript
        if sentence:
            # New speech arrived — cancel any pending response (user is still talking)
            if pending_response_task and not pending_response_task.done():
                pending_response_task.cancel()
                pending_response_task = None

            # Signal barge-in if agent is currently playing audio
            if agent_speaking:
                barge_in.set()

            utterance_buffer.append(sentence)
            # Keep a warm semantic end-of-turn probability: by the time
            # speech_final arrives, the classifier has usually already
            # scored the utterance, so the endpoint decision is instant.
            turn_detector.observe_final(sentence)

        if result.speech_final:
            full_utterance = " ".join(utterance_buffer).strip()
            utterance_buffer.clear()

            if not full_utterance:
                return

            # Skip filler-only utterances — keep waiting for real content
            if _is_only_filler(full_utterance):
                logger.info("Call %s — skipping filler-only segment: '%s'", call_id, full_utterance)
                utterance_buffer.append(full_utterance)  # re-buffer so it joins the next real sentence
                return

            # Schedule a debounced response instead of responding immediately
            if pending_response_task and not pending_response_task.done():
                pending_response_task.cancel()
            pending_response_task = asyncio.create_task(
                _schedule_response(full_utterance)
            )

    # ── Deepgram lifecycle event handlers ──
    async def on_dg_open(self, open_response, **kwargs):
        logger.info("Call %s — Deepgram WebSocket OPEN (connection ready)", call_id)

    async def on_dg_error(self, error, **kwargs):
        logger.error("Call %s — Deepgram ERROR: %s", call_id, error)

    async def on_dg_close(self, *args, **kwargs):
        logger.info("Call %s — Deepgram WebSocket CLOSED", call_id)

    async def on_speech_started(self, speech_started, **kwargs):
        """Deepgram detected the start of speech — trigger barge-in if agent is talking.

        This fires the instant Deepgram's VAD detects voice energy, BEFORE any
        transcript is produced.  Much faster than waiting for on_transcript.

        The provider's audio buffer is flushed HERE, immediately, rather than
        waiting for the playback loop to notice the barge-in flag — the flush
        loop can be blocked on a slow TTS chunk, and every extra 100ms of the
        agent talking over the human is perceived as an interruption failure.
        """
        # Track first speech from callee (used by outbound silence fallback)
        if not first_speech_received.is_set():
            first_speech_received.set()
            logger.debug("Call %s — first speech detected from callee", call_id)

        if agent_speaking:
            barge_in.set()
            # Flush the provider's buffer HERE so the agent goes silent
            # immediately — but only once playback is established: the
            # first moments of agent audio can echo back on analogue
            # lines and must not self-interrupt (see MIN_BARGEIN_CLEAR_DELAY).
            if (
                agent_speaking_since
                and time.monotonic() - agent_speaking_since >= MIN_BARGEIN_CLEAR_DELAY
            ):
                await clear_audio(provider_ws, stream_sid)
                logger.debug("Call %s — SpeechStarted: barge-in signalled, provider buffer cleared", call_id)
            else:
                logger.debug("Call %s — SpeechStarted: barge-in signalled", call_id)

    async def on_utterance_end(self, utterance_end, **kwargs):
        nonlocal pending_response_task
        logger.info("Call %s — Deepgram UtteranceEnd event (buffer: %s)", call_id, utterance_buffer)

        # If voicemail was detected, this UtteranceEnd means the greeting
        # finished playing (i.e. the beep happened).  Signal the voicemail
        # handler so it can start leaving the message.
        if voicemail_detected.is_set():
            voicemail_greeting_ended.set()
            logger.info("Call %s — voicemail greeting ended (beep detected via UtteranceEnd)", call_id)
            return

        # Fallback: if we have buffered text but speech_final never fired, flush now
        if utterance_buffer:
            full_utterance = " ".join(utterance_buffer).strip()
            utterance_buffer.clear()
            if full_utterance and not _is_only_filler(full_utterance):
                logger.info("Call %s — Human (via UtteranceEnd): %s", call_id, full_utterance)

                # Schedule debounced response (same as on_transcript path)
                if pending_response_task and not pending_response_task.done():
                    pending_response_task.cancel()
                pending_response_task = asyncio.create_task(
                    _schedule_response(full_utterance)
                )
            elif full_utterance:
                logger.info("Call %s — skipping filler-only UtteranceEnd: '%s'", call_id, full_utterance)

    dg_connection.on(LiveTranscriptionEvents.Open, on_dg_open)
    dg_connection.on(LiveTranscriptionEvents.Error, on_dg_error)
    dg_connection.on(LiveTranscriptionEvents.Close, on_dg_close)
    dg_connection.on(LiveTranscriptionEvents.SpeechStarted, on_speech_started)
    dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
    dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
    options = get_stt_options()
    result = await dg_connection.start(options)
    logger.info("Deepgram STT start() returned for call %s: %s", call_id, result)

    media_frame_count = 0

    # Forward audio from Provider → Deepgram and handle stream lifecycle
    try:
        async for message in provider_ws.iter_text():
            data = json.loads(message)
            event = data.get("event", "")

            if event == "media":
                # Both SignalWire and Plivo send audio in {"event":"media","media":{"payload":"..."}}
                audio_payload = data.get("media", {}).get("payload", "")
                if audio_payload:
                    audio_bytes = base64.b64decode(audio_payload)
                    try:
                        await dg_connection.send(audio_bytes)
                        media_frame_count += 1
                        if media_frame_count in (1, 10, 50, 100):
                            logger.info(
                                "Call %s — forwarded %d media frames to Deepgram (%d bytes this frame)",
                                call_id, media_frame_count, len(audio_bytes),
                            )
                    except Exception as e:
                        logger.error("Call %s — failed to send audio to Deepgram: %s", call_id, e)

            elif event == "connected":
                # SignalWire sends 'connected' first — just the WebSocket handshake
                # Do NOT send greeting yet — we need streamSid from 'start' event
                logger.info("WebSocket connected for call %s (waiting for stream start...)", call_id)

            elif event == "start":
                # SignalWire sends 'start' with stream metadata including streamSid
                # This is when the audio stream is actually ready
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid", "") or data.get("streamSid", "")
                logger.info(
                    "Stream started for call %s (streamSid: %s, tracks: %s)",
                    call_id, stream_sid,
                    start_data.get("tracks", "unknown"),
                )

                # ── Direction-aware greeting ──────────────────────────
                if call_direction == "inbound":
                    # Inbound: greet immediately (caller is waiting)
                    if initial_greeting and not greeting_sent:
                        try:
                            logger.info("Sending greeting for call %s with voice %s", call_id, voice_id)
                            audio = await tts_cartesia(initial_greeting, voice_id)
                            await send_audio(provider_ws, audio, stream_sid)
                            transcript_turns.append({
                                "role": "agent",
                                "text": initial_greeting,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                            greeting_sent = True
                            logger.info("Greeting sent for call %s (%d bytes audio)", call_id, len(audio))
                        except Exception as e:
                            logger.error("Failed to send greeting for call %s: %s", call_id, e)
                            greeting_sent = True
                else:
                    # Outbound: listen first — suppress greeting, start silence fallback
                    greeting_sent = True  # Prevent greeting from firing later
                    logger.info(
                        "Call %s — outbound mode: listening first (greeting suppressed, "
                        "silence fallback in 2s)",
                        call_id,
                    )
                    asyncio.create_task(_outbound_silence_fallback())

            elif event == "stop":
                logger.info("%s sent stop event for call %s (received %d media frames total)", provider.capitalize(), call_id, media_frame_count)
                break

            else:
                logger.debug("Call %s — unknown event: %s", call_id, event)

    except Exception as e:
        logger.info("WebSocket closed for call %s: %s (received %d media frames)", call_id, e, media_frame_count)
    finally:
        if pending_response_task and not pending_response_task.done():
            pending_response_task.cancel()
            try:
                await pending_response_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await dg_connection.finish()
        except Exception as e:
            logger.debug("Deepgram finish error (expected on disconnect): %s", e)

        # Save final transcript to DB
        try:
            async with get_db_conn() as db:
                await db.execute(
                    """UPDATE calls
                       SET transcript=$1, ended_at=now()
                       WHERE id=$2""",
                    json.dumps(transcript_turns),
                    call_id,
                )
        except Exception as e:
            logger.warning("Failed to save final transcript for call %s: %s", call_id, e)

        logger.info(
            "Pipeline finished for call %s — %d turns",
            call_id, len(transcript_turns),
        )
