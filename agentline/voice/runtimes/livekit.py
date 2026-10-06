"""LiveKit voice runtime.

Two ways to attach the phone call:

1. SIP. Set ``LIVEKIT_SIP_URI``. Agentline creates a room, dispatches
   ``LIVEKIT_AGENT_NAME``, and tells the carrier to dial the SIP URI.
   The URI may contain ``{room}``, ``{call_id}``, ``{from_number}``, and
   ``{to_number}``. Point the LiveKit SIP trunk at that room name.

2. Media bridge. Leave the SIP URI empty. Agentline keeps the carrier
   websocket and publishes the caller into the LiveKit room. This needs
   ``pip install -r requirements-livekit.txt``, or your own bridge:

       from agentline.voice.runtimes.livekit import register_livekit_bridge
       register_livekit_bridge(my_bridge)

   ``my_bridge`` is ``async def my_bridge(websocket, ctx)``.

Room and dispatch metadata is JSON with the call id, prompt, greeting,
voice, and phone numbers. Your LiveKit agent reads that metadata.
"""

import importlib.util
import json
import logging
import time

import httpx
from jose import jwt

from agentline.config import settings
from agentline.voice.runtime import AnswerPlan, CallContext

logger = logging.getLogger(__name__)

_bridge = None


def register_livekit_bridge(factory) -> None:
    """Replace the default carrier-to-room audio bridge."""
    global _bridge
    _bridge = factory
    logger.info("Registered LiveKit media bridge")


def format_sip_uri(template: str, ctx: CallContext, room: str) -> str:
    values = {
        "room": room,
        "call_id": ctx.call_id,
        "from_number": ctx.from_number,
        "to_number": ctx.to_number,
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value or "")
    return rendered


def _http_url(url: str) -> str:
    return (
        url.replace("wss://", "https://")
        .replace("ws://", "http://")
        .rstrip("/")
    )


def _token(room: str | None = None) -> str:
    now = int(time.time())
    video = {
        "roomCreate": True,
        "roomAdmin": True,
        "roomList": True,
        "roomJoin": True,
        "canPublish": True,
        "canSubscribe": True,
    }
    if room:
        video["room"] = room
    token = jwt.encode(
        {
            "iss": settings.LIVEKIT_API_KEY,
            "sub": "agentline",
            "nbf": now - 5,
            "exp": now + 600,
            "video": video,
        },
        settings.LIVEKIT_API_SECRET,
        algorithm="HS256",
    )
    if isinstance(token, bytes):
        return token.decode()
    return token


def _metadata(ctx: CallContext, room: str) -> str:
    return json.dumps({
        "room": room,
        "call_id": ctx.call_id,
        "system_prompt": ctx.system_prompt,
        "initial_greeting": ctx.initial_greeting,
        "voice_id": ctx.voice_id,
        "model_tier": ctx.model_tier,
        "from_number": ctx.from_number,
        "to_number": ctx.to_number,
        "direction": ctx.direction,
        "media": ctx.media,
    })


def _configured() -> bool:
    return bool(settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET)


def _sdk_installed() -> bool:
    return importlib.util.find_spec("livekit") is not None


class LiveKitRuntime:
    name = "livekit"

    async def prepare(self, ctx: CallContext) -> AnswerPlan:
        if not _configured():
            raise RuntimeError(
                "LiveKit runtime needs LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET."
            )
        room = f"call-{ctx.call_id}"
        meta = _metadata(ctx, room)
        await self._create_room(room, meta)
        if settings.LIVEKIT_AGENT_NAME:
            await self._dispatch(room, meta)
        if settings.LIVEKIT_SIP_URI:
            return AnswerPlan(
                mode="sip",
                sip_uri=format_sip_uri(settings.LIVEKIT_SIP_URI, ctx, room),
            )
        if _bridge is None and not _sdk_installed():
            raise RuntimeError(
                "LiveKit has no media path. Set LIVEKIT_SIP_URI, "
                "pip install -r requirements-livekit.txt, "
                "or call register_livekit_bridge()."
            )
        ctx.extra["livekit_room"] = room
        return AnswerPlan(mode="stream")

    async def run(self, websocket, ctx: CallContext) -> None:
        if _bridge is not None:
            bridge = _bridge() if isinstance(_bridge, type) else _bridge
            await bridge(websocket, ctx)
            return
        await _sdk_bridge(websocket, ctx)

    async def _create_room(self, room: str, metadata: str) -> None:
        await _twirp("livekit.RoomService/CreateRoom", {"name": room, "metadata": metadata, "empty_timeout": 300})

    async def _dispatch(self, room: str, metadata: str) -> None:
        await _twirp(
            "livekit.AgentDispatchService/CreateDispatch",
            {
                "agent_name": settings.LIVEKIT_AGENT_NAME,
                "room": room,
                "metadata": metadata,
            },
        )


async def _twirp(method: str, body: dict) -> dict:
    url = f"{_http_url(settings.LIVEKIT_URL)}/twirp/{method}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"LiveKit {method} failed ({response.status_code}): {response.text[:400]}")
        if not response.content:
            return {}
        return response.json()


async def _sdk_bridge(websocket, ctx: CallContext) -> None:
    """Publish carrier mulaw into a LiveKit room and play the agent back."""
    import asyncio
    import json as jsonlib

    from livekit import rtc

    from agentline.voice.media import sender_for
    from agentline.voice.mulaw import pcm16_to_ulaw, ulaw_to_pcm16

    room_name = ctx.extra.get("livekit_room") or f"call-{ctx.call_id}"
    room = rtc.Room()
    token = _token(room_name)
    await room.connect(settings.LIVEKIT_URL, token)

    source = rtc.AudioSource(8000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("caller", source)
    await room.local_participant.publish_track(track)
    send_audio = sender_for(ctx.media)
    stream_sid = ""

    async def _forward_agent(remote_track):
        audio_stream = rtc.AudioStream(remote_track, sample_rate=8000, num_channels=1)
        async for event in audio_stream:
            pcm = bytes(event.frame.data)
            await send_audio(websocket, pcm16_to_ulaw(pcm), stream_sid)

    @room.on("track_subscribed")
    def _on_track(remote_track, publication, participant):
        if remote_track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(_forward_agent(remote_track))

    try:
        async for message in websocket.iter_text():
            data = jsonlib.loads(message)
            event = data.get("event", "")
            if event == "start":
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid", "") or data.get("streamSid", "")
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                if not payload:
                    continue
                import base64
                pcm = ulaw_to_pcm16(base64.b64decode(payload))
                frame = rtc.AudioFrame(pcm, 8000, 1, len(pcm) // 2)
                await source.capture_frame(frame)
            elif event == "stop":
                break
    finally:
        await room.disconnect()
