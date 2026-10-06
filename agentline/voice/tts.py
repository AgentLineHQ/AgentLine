"""
AgentLine — Cartesia TTS (Streaming + HTTP Fallback)
Text-to-speech using Cartesia's Sonic model, outputting mulaw 8kHz for telephony.

Two modes:
  1. WebSocket streaming (primary) — yields audio chunks as they're synthesized,
     dramatically reducing time-to-first-audio.  Uses the official Cartesia SDK.
  2. HTTP bytes (fallback) — returns the full audio blob in one shot.
     Used for greetings and when the WebSocket connection fails.
"""

import asyncio
import logging
from typing import AsyncGenerator

import httpx

from agentline.config import settings

try:
    from cartesia import AsyncCartesia
except ImportError:  # optional — HTTP TTS still works without the SDK
    AsyncCartesia = None

logger = logging.getLogger(__name__)

# Cartesia API version — must match current API
CARTESIA_API_VERSION = "2026-03-01"

# Model — sonic-3.5 is the latest stable model
CARTESIA_MODEL = "sonic-3.5"

# Output format for telephony (mulaw 8kHz mono)
_OUTPUT_FORMAT = {
    "container": "raw",
    "encoding": "pcm_mulaw",
    "sample_rate": 8000,
}


# ── Persistent Cartesia SDK client ────────────────────────────────
_cartesia_client: AsyncCartesia | None = None


def _get_cartesia_client():
    """Return a long-lived AsyncCartesia client, creating one if needed."""
    if AsyncCartesia is None:
        raise RuntimeError("cartesia SDK is not installed")
    global _cartesia_client
    if _cartesia_client is None:
        _cartesia_client = AsyncCartesia(api_key=settings.CARTESIA_API_KEY)
    return _cartesia_client


# ── WebSocket connection manager ──────────────────────────────────
# We keep a single WebSocket connection open and reuse it across TTS
# requests.  The Cartesia SDK supports multiplexing via "contexts" on
# a single connection.  If the connection drops, we reconnect lazily.
#
# NOTE: the correct SDK method is ``client.tts.websocket_connect()`` (an
# async context manager).  ``client.tts.websocket()`` is the JS SDK name;
# in the Python SDK it is an unawaited coroutine, which is what previously
# caused "'coroutine' object has no attribute '__aenter__'" on every call
# and forced the HTTP fallback for every TTS turn.

_ws_cm = None          # the AsyncContextManager returned by websocket_connect()
_ws_connection = None  # the live ws resource (ws_cm.__aenter__())
_ws_lock = asyncio.Lock()


async def _get_ws_connection():
    """Get or create a persistent Cartesia WebSocket connection."""
    global _ws_cm, _ws_connection
    async with _ws_lock:
        if _ws_connection is None:
            client = _get_cartesia_client()
            _ws_cm = client.tts.websocket_connect()
            _ws_connection = await _ws_cm.__aenter__()
            logger.info("Cartesia WebSocket connection established")
        return _ws_connection


async def _reset_ws_connection():
    """Close and discard the current WebSocket connection."""
    global _ws_cm, _ws_connection
    async with _ws_lock:
        if _ws_cm is not None:
            try:
                await _ws_cm.__aexit__(None, None, None)
            except Exception:
                pass
            _ws_cm = None
            _ws_connection = None
            logger.info("Cartesia WebSocket connection reset")


async def prewarm_cartesia_connection() -> None:
    """Open the shared Cartesia WebSocket without synthesizing any audio."""
    if AsyncCartesia is None:
        return
    try:
        await _get_ws_connection()
    except Exception as e:
        logger.warning("Cartesia WebSocket prewarm failed: %s", e)
        await _reset_ws_connection()


# ── Streaming TTS (primary) ──────────────────────────────────────

async def tts_cartesia_stream(text: str, voice_id: str) -> AsyncGenerator[bytes, None]:
    """Stream TTS audio chunks via Cartesia WebSocket.

    Yields raw pcm_mulaw audio chunks as they're synthesized, allowing
    the caller to forward them to the telephony provider immediately.
    This is the low-latency path.

    Falls back to the HTTP endpoint on WebSocket errors.
    """
    if not text or not text.strip():
        logger.warning("TTS stream called with empty text, skipping")
        return

    if AsyncCartesia is None:
        async for chunk in _tts_http_stream_fallback(text, voice_id):
            yield chunk
        return

    try:
        ws = await _get_ws_connection()
        ctx = ws.context(
            model_id=CARTESIA_MODEL,
            voice={"mode": "id", "id": voice_id},
            output_format=_OUTPUT_FORMAT,
            language="en",
        )
        await ctx.push(text)
        await ctx.no_more_inputs()

        total_bytes = 0
        async for response in ctx.receive():
            if response.type == "chunk" and response.audio:
                total_bytes += len(response.audio)
                yield bytes(response.audio)
            elif response.type == "error":
                logger.error(
                    "Cartesia WS TTS error: %s — %s",
                    getattr(response, "title", "unknown"),
                    getattr(response, "message", ""),
                )
                # Context-scoped error (e.g. a bad voice id for THIS call) —
                # fall back to HTTP without touching the shared connection;
                # resetting here would break every other live call mid-sentence.
                async for chunk in _tts_http_stream_fallback(text, voice_id):
                    yield chunk
                return

        logger.debug("TTS streamed %d bytes for %d chars", total_bytes, len(text))

    except Exception as e:
        logger.warning("Cartesia WS TTS failed (%s), falling back to HTTP", e)
        await _reset_ws_connection()
        # Yield the full blob from HTTP as a single chunk
        async for chunk in _tts_http_stream_fallback(text, voice_id):
            yield chunk


async def _tts_http_stream_fallback(text: str, voice_id: str) -> AsyncGenerator[bytes, None]:
    """HTTP fallback — yields the complete audio as a single chunk."""
    audio = await tts_cartesia(text, voice_id)
    if audio:
        yield audio


# ── HTTP TTS (fallback / greetings) ──────────────────────────────

# Persistent HTTP client — reuses TCP + TLS connections
_http_client: httpx.AsyncClient | None = None

_CARTESIA_HEADERS = {
    "X-API-Key": settings.CARTESIA_API_KEY,
    "Cartesia-Version": CARTESIA_API_VERSION,
    "Content-Type": "application/json",
}


def _get_http_client() -> httpx.AsyncClient:
    """Return a long-lived httpx client, creating one if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=30.0,
        )
    return _http_client


class CartesiaTTS:
    """Mulaw TTS. Selected when ``TTS_PROVIDER=cartesia``."""

    name = "cartesia"

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        return await tts_cartesia(text, voice_id)


async def tts_cartesia(text: str, voice_id: str) -> bytes:
    """
    Convert text to mulaw 8kHz audio via Cartesia HTTP API.
    Returns raw audio bytes ready for telephony media stream.
    Used for greetings and as a fallback when WebSocket is unavailable.
    """
    if not text or not text.strip():
        logger.warning("TTS called with empty text, skipping")
        return b""

    payload = {
        "model_id": CARTESIA_MODEL,
        "transcript": text,
        "voice": {"id": voice_id},
        "language": "en",
        "output_format": {
            "container": "raw",
            "encoding": "pcm_mulaw",
            "sample_rate": 8000,
        },
    }

    client = _get_http_client()
    try:
        response = await client.post(
            "https://api.cartesia.ai/tts/bytes",
            headers=_CARTESIA_HEADERS,
            json=payload,
        )
        response.raise_for_status()
        logger.debug("TTS generated %d bytes for %d chars", len(response.content), len(text))
        return response.content
    except httpx.HTTPStatusError as e:
        # Log the actual error body so we can debug
        error_body = e.response.text[:500]
        logger.error(
            "Cartesia TTS failed (HTTP %d): %s | Payload: model=%s voice=%s text='%s'",
            e.response.status_code, error_body,
            CARTESIA_MODEL, voice_id, text[:80],
        )
        raise
    except Exception as e:
        logger.error("Cartesia TTS error: %s", e)
        raise


# ── Cleanup ──────────────────────────────────────────────────────

async def close_tts():
    """Close all TTS connections. Call at app shutdown."""
    await _reset_ws_connection()
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
    global _cartesia_client
    if _cartesia_client:
        await _cartesia_client.close()
        _cartesia_client = None
    logger.info("TTS connections closed")
