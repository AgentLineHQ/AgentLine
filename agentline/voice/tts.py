"""
AgentLine — Cartesia Streaming TTS
Text-to-speech using Cartesia's Sonic model, outputting mulaw 8kHz for telephony.

Uses Sonic 3.5 (latest) with pcm_mulaw encoding for direct telephony compatibility.
"""

import logging
import httpx
from agentline.config import settings

logger = logging.getLogger(__name__)

# Cartesia API version — must match current API
CARTESIA_API_VERSION = "2026-03-01"

# Model — sonic-3.5 is the latest stable model
CARTESIA_MODEL = "sonic-3.5"

# ── Persistent HTTP client ────────────────────────────────────────
# Reuses TCP + TLS connections across calls.  Eliminates ~300-500ms
# handshake overhead that the old `async with httpx.AsyncClient()` pattern
# paid on every single TTS invocation.
_http_client: httpx.AsyncClient | None = None

_CARTESIA_HEADERS = {
    "X-API-Key": settings.CARTESIA_API_KEY,
    "Cartesia-Version": CARTESIA_API_VERSION,
    "Content-Type": "application/json",
}


def _get_client() -> httpx.AsyncClient:
    """Return a long-lived httpx client, creating one if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=30.0,
        )
    return _http_client


async def tts_cartesia(text: str, voice_id: str) -> bytes:
    """
    Convert text to mulaw 8kHz audio via Cartesia API.
    Returns raw audio bytes ready for telephony media stream.
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

    client = _get_client()
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
