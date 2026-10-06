"""
AgentLine — Built-in voice pipeline.

Carrier audio → STT hook → LLM hook → TTS hook → carrier audio.

The default hooks are Deepgram, an OpenAI-compatible chat model, and
Cartesia. Swap any of them with ``register_stt``, ``register_llm``, and
``register_tts``. Carrier framing is selected with ``provider``
(``signalwire``, ``twilio``, ``telnyx``, or ``plivo``).
"""

import asyncio
import json
import base64
import logging
from datetime import datetime, timezone

from agentline.database import get_db_conn
from agentline.voice.hooks import get_llm, get_stt, get_tts
from agentline.voice.media import sender_for
from agentline.voice.voices import resolve_voice_id

logger = logging.getLogger(__name__)

# ── Turn-taking tuning ────────────────────────────────────────────
# How long to wait (seconds) after Deepgram signals speech_final before
# actually triggering the LLM.  If the user resumes speaking within this
# window the timer is cancelled and the new words are appended.
DEBOUNCE_SECONDS = 0.9

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





async def run_pipeline(
    provider_ws,
    call_id: str,
    system_prompt: str,
    initial_greeting: str | None,
    voice_id: str,
    model_tier: str,
    provider: str = "signalwire",
):
    """
    Main voice pipeline coroutine. One instance per active call.
    Bridges carrier audio with the configured STT, LLM, and TTS hooks.

    Args:
        provider_ws: WebSocket connection to the telephony provider
        call_id: Internal call ID
        system_prompt: System prompt for the LLM
        initial_greeting: Optional greeting to speak when call starts
        voice_id: Voice id understood by the active TTS hook
        model_tier: LLM model tier (turbo/balanced/max)
        provider: Media framing name (signalwire, twilio, telnyx, plivo)
    """
    voice_id = resolve_voice_id(voice_id)
    send_audio = sender_for(provider)
    synthesizer = get_tts()
    language_model = get_llm()

    conversation_history: list[dict] = []
    transcript_turns: list[dict] = []
    stream_sid = ""  # Set when we receive the 'start' event with metadata
    greeting_sent = False
    pending_response_task: asyncio.Task | None = None  # debounce handle

    utterance_buffer: list[str] = []

    # ── Speculative execution helpers ─────────────────────────────
    async def _speculative_generate(
        utterance: str,
        audio_queue: asyncio.Queue,
    ):
        """Stream LLM → TTS, buffering audio into *audio_queue*.

        Runs concurrently with the debounce timer so audio is ready
        the instant the debounce expires.  A ``None`` sentinel is put
        into the queue when generation finishes (or on error).
        """
        try:
            async for sentence in language_model.stream(
                system_prompt, conversation_history, model_tier
            ):
                try:
                    audio = await synthesizer.synthesize(sentence, voice_id)
                    await audio_queue.put((audio, sentence))
                except Exception as e:
                    logger.error("Call %s — TTS failed during speculative gen: %s", call_id, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Call %s — speculative generation error: %s", call_id, e)
        finally:
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
        nonlocal pending_response_task

        audio_queue: asyncio.Queue = asyncio.Queue()
        committed = False  # tracks whether we've committed the turn

        # Tentatively add user message so the LLM has context
        conversation_history.append({"role": "user", "content": utterance})

        # Fire off LLM → TTS generation immediately (don't wait for debounce)
        gen_task = asyncio.create_task(
            _speculative_generate(utterance, audio_queue)
        )

        try:
            # ── Phase 1: Debounce ─────────────────────────────────
            await asyncio.sleep(DEBOUNCE_SECONDS)

            # User stayed silent → commit the human turn
            committed = True
            logger.info("Call %s — Human: %s", call_id, utterance)
            transcript_turns.append({
                "role": "human",
                "text": utterance,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            try:
                async with get_db_conn() as db:
                    await db.execute(
                        "UPDATE calls SET transcript=$1 WHERE id=$2",
                        json.dumps(transcript_turns), call_id,
                    )
            except Exception as e:
                logger.warning("Failed to save transcript for call %s: %s", call_id, e)

            # ── Phase 2: Flush buffered audio ─────────────────────
            reply_parts: list[str] = []
            while True:
                item = await audio_queue.get()
                if item is None:  # sentinel — generation done
                    break
                audio, sentence = item
                reply_parts.append(sentence)
                logger.debug("Call %s — flushing sentence: %s", call_id, sentence[:80])
                try:
                    await send_audio(provider_ws, audio, stream_sid)
                except Exception as e:
                    logger.error("Call %s — send audio failed: %s", call_id, e)

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
            try:
                async with get_db_conn() as db:
                    await db.execute(
                        "UPDATE calls SET transcript=$1 WHERE id=$2",
                        json.dumps(transcript_turns), call_id,
                    )
            except Exception as e:
                logger.warning("Failed to save transcript for call %s: %s", call_id, e)

        except asyncio.CancelledError:
            # User resumed speaking — discard speculative work
            gen_task.cancel()
            try:
                await gen_task
            except (asyncio.CancelledError, Exception):
                pass
            # Roll back the tentative user message if we haven't committed yet
            if not committed:
                for i in range(len(conversation_history) - 1, -1, -1):
                    if conversation_history[i] == {"role": "user", "content": utterance}:
                        conversation_history.pop(i)
                        break
            logger.debug("Call %s — speculative response discarded (user resumed speaking)", call_id)
            raise

        finally:
            pending_response_task = None

    # Fired by the STT hook for each finalized transcript segment.
    async def on_transcript(sentence: str, speech_final: bool):
        nonlocal pending_response_task

        if sentence:
            # New speech arrived — cancel any pending response (user is still talking)
            if pending_response_task and not pending_response_task.done():
                pending_response_task.cancel()
                pending_response_task = None
            utterance_buffer.append(sentence)

        if speech_final:
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

    async def on_utterance_end():
        nonlocal pending_response_task
        logger.info("Call %s — STT utterance end (buffer: %s)", call_id, utterance_buffer)
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

    stt_provider = get_stt()
    stt = stt_provider.open()
    stt.on_transcript(on_transcript)
    stt.on_utterance_end(on_utterance_end)
    await stt.start()
    logger.info("STT started for call %s (%s)", call_id, getattr(stt_provider, "name", "custom"))

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
                        await stt.send(audio_bytes)
                        media_frame_count += 1
                        if media_frame_count in (1, 10, 50, 100):
                            logger.info(
                                "Call %s — forwarded %d media frames to STT (%d bytes this frame)",
                                call_id, media_frame_count, len(audio_bytes),
                            )
                    except Exception as e:
                        logger.error("Call %s — failed to send audio to STT: %s", call_id, e)

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

                # NOW send the initial greeting — stream is ready
                if initial_greeting and not greeting_sent:
                    try:
                        logger.info("Sending greeting for call %s with voice %s", call_id, voice_id)
                        audio = await synthesizer.synthesize(initial_greeting, voice_id)
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
                        # Don't mark as sent — but don't retry either to avoid loops
                        greeting_sent = True

            elif event == "stop":
                logger.info("%s sent stop event for call %s (received %d media frames total)", provider.capitalize(), call_id, media_frame_count)
                break

            else:
                logger.debug("Call %s — unknown event: %s", call_id, event)

    except Exception as e:
        logger.info("WebSocket closed for call %s: %s (received %d media frames)", call_id, e, media_frame_count)
    finally:
        try:
            await stt.finish()
        except Exception as e:
            logger.debug("STT finish error (expected on disconnect): %s", e)

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
