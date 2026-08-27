"""Shared parsing helpers for external agent context responses."""

import asyncio
import json
import re


def speech_segments(text: str) -> list[str]:
    """Split caller-ready relay text into transcript-safe spoken sentences."""
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]


async def queue_relay_speech(
    context: str,
    voice_id: str,
    audio_queue: asyncio.Queue,
    tts_stream,
) -> int:
    """Stream caller-ready relay text to playback without rewriting it."""
    chunks_queued = 0
    for segment in speech_segments(context):
        async for audio_chunk in tts_stream(segment, voice_id):
            await audio_queue.put((audio_chunk, segment))
            chunks_queued += 1
    return chunks_queued


async def queue_relay_acknowledgement(
    text: str,
    voice_id: str,
    audio_queue: asyncio.Queue,
    tts_stream,
    context_ready: asyncio.Event,
    *,
    delay: float,
) -> bool:
    """Queue one complete acknowledgement unless the relay answers first."""
    await asyncio.sleep(delay)
    chunks = [chunk async for chunk in tts_stream(text, voice_id)]
    if context_ready.is_set() or not chunks:
        return False
    for chunk in chunks:
        await audio_queue.put((chunk, text))
    return True


def extract_context(result) -> str | None:
    """Extract context from the canonical field or common agent output fields."""
    if not result:
        return None
    if isinstance(result, str):
        value = result.strip()
        return value or None
    if isinstance(result, dict):
        # ``context`` is canonical. Compatibility aliases make generic agent
        # and SDK adapters easier to connect without response translation.
        for key in (
            "context", "summary", "answer", "response",
            "reply", "text", "result", "message",
        ):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                if key == "message" and value.strip().lower() in {
                    "accepted", "ok", "queued", "received",
                }:
                    continue
                return value.strip()
            if isinstance(value, (dict, list)) and value:
                return json.dumps(value, default=str)
    return None
