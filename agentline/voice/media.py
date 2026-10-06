"""Carrier websocket audio framing.

SignalWire, Twilio, and Telnyx TeXML share the Media Streams JSON shape.
Plivo uses ``playAudio``. Voice runtimes send caller audio through these
helpers so a new carrier only needs a new sender.
"""

import base64
import logging

logger = logging.getLogger(__name__)


async def send_audio_twilio_media(ws, audio_bytes: bytes, stream_sid: str):
    """Send mulaw audio on a Twilio / SignalWire / Telnyx media stream."""
    if not audio_bytes:
        return
    payload = base64.b64encode(audio_bytes).decode("ascii")
    await ws.send_json({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload},
    })


async def send_audio_plivo(ws, audio_bytes: bytes, _stream_sid: str = ""):
    """Send mulaw audio on a Plivo bidirectional stream."""
    if not audio_bytes:
        return
    payload = base64.b64encode(audio_bytes).decode("ascii")
    await ws.send_json({
        "event": "playAudio",
        "media": {"payload": payload, "contentType": "audio/x-mulaw;rate=8000"},
    })


SENDERS = {
    "signalwire": send_audio_twilio_media,
    "twilio": send_audio_twilio_media,
    "telnyx": send_audio_twilio_media,
    "plivo": send_audio_plivo,
}


def sender_for(media: str):
    send = SENDERS.get(media)
    if send is None:
        logger.warning("Unknown media framing '%s' — using Twilio media JSON.", media)
        return send_audio_twilio_media
    return send
