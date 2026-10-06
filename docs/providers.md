# Providers and voice runtimes

Agentline ships the call API, the event mailbox, and the webhook surface. The carrier and the voice stack are hooks. You pay those vendors yourself. This tree has no balance ledger and no Supabase project.

`GET /debug/urls` shows which hooks are active and the callback URLs to paste into a carrier dashboard.

## Telephony

`TELEPHONY_PROVIDER` selects who buys numbers and places calls.

| Value | What you set |
| --- | --- |
| `signalwire` | `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_TOKEN`, `SIGNALWIRE_SPACE_URL` |
| `twilio` | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` |
| `plivo` | `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, and a Plivo application id in `PLIVO_APP_ID` |
| `telnyx` | `TELNYX_API_KEY`, `TELNYX_ACCOUNT_SID` (TeXML app), `TELNYX_CONNECTION_ID` |
| `package.module:Class` | Your own class |

Webhook routes for all four built-ins stay mounted. A number keeps working after you change `TELEPHONY_PROVIDER`, because each number stores the carrier that sold it.

Point the carrier at these paths (`BASE_URL` is your public origin):

| Carrier | Inbound voice | Inbound SMS | Hangup |
| --- | --- | --- | --- |
| SignalWire, Twilio, Telnyx | `POST /{carrier}/inbound` | `POST /{carrier}/sms` | `POST /{carrier}/inbound_hangup` |
| Plivo | the Plivo application's answer URL `POST /plivo/inbound` | `POST /plivo/sms` | `POST /plivo/inbound_hangup` |

### Your own carrier

Implement the methods on `SignalWireProvider` (see `agentline/providers/signalwire.py`) and either register it or point the env var at the class.

```python
from agentline.providers import register_telephony

class AcmeProvider:
    name = "acme"
    media = "twilio"  # signalwire, twilio, and telnyx share one audio framing

    def is_configured(self) -> bool: ...
    def stream_xml(self, call_id: str) -> str: ...
    def sip_dial_xml(self, sip_uri: str, headers: dict | None = None) -> str: ...
    def say_xml(self, text: str) -> str: ...
    async def initiate_call(self, from_number, to_number, call_id) -> str: ...
    async def hangup_call(self, provider_call_id: str) -> None: ...
    async def send_sms(self, from_number, to_number, body, media_url=None): ...
    async def provision_number(self, country="US", number_type="local", area_code=None, pattern=None, agent_id=None): ...
    async def release_number(self, provider_id: str) -> None: ...
    async def configure_number(self, provider_id: str) -> None: ...
    async def set_status_callback(self, provider_call_id: str, call_id: str) -> None: ...
    async def reconfigure_active_numbers(self) -> None: ...

register_telephony("acme", AcmeProvider)
```

`media` tells the voice pipeline how to send audio back. Use `twilio` for Media Streams JSON (`event: media`, `streamSid`). Use `plivo` for Plivo's `playAudio` messages.

`LamlMedia` in `agentline/providers/base.py` builds the Twilio-style XML for you.

Register from a module you import at startup, or skip registration and set:

```
TELEPHONY_PROVIDER=myapp.acme:AcmeProvider
```

## Voice runtime

`VOICE_RUNTIME` selects who holds the conversation. Set `voice_runtime` on an agent to override it for that agent only (`builtin`, `livekit`, `pipecat`, or `module:Class`).

### Built-in

`VOICE_RUNTIME=builtin` runs the in-process pipeline: Deepgram, semantic turn-taking, an OpenAI-compatible LLM (or live relay context), and streaming Cartesia. That path calls those clients directly so barge-in and streaming audio stay intact.

`get_stt()`, `get_tts()`, and `get_llm()` are the extension points for your own runtime. Register a replacement or point the env var at `module:Class`.

| Hook | Default | Env |
| --- | --- | --- |
| Speech-to-text | Deepgram | `STT_PROVIDER` |
| Language model | OpenAI-compatible (`OPENAI_BASE_URL`) | `LLM_PROVIDER` |
| Speech synthesis | Cartesia, 8 kHz mulaw | `TTS_PROVIDER` |

```python
from agentline.voice.hooks import register_stt, register_tts, register_llm

class MyTTS:
    name = "my-tts"
    async def synthesize(self, text: str, voice_id: str) -> bytes:
        return b""  # 8 kHz mulaw

register_tts("my-tts", MyTTS)
```

An STT object implements `open()` and returns a session with `on_transcript(callback)`, `on_utterance_end(callback)`, `start()`, `send(audio)`, and `finish()`. The transcript callback is `async callback(text: str, speech_final: bool)`.

An LLM object implements `stream(system_prompt, history, model_tier)` and yields sentence-sized strings.

### LiveKit

`VOICE_RUNTIME=livekit`

Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and `LIVEKIT_AGENT_NAME`. On answer, Agentline creates room `call-{call_id}` and dispatches that agent. Room metadata is JSON:

```json
{
  "call_id": "call_...",
  "system_prompt": "...",
  "initial_greeting": "...",
  "voice_id": "female-1",
  "from_number": "+1...",
  "to_number": "+1...",
  "direction": "inbound"
}
```

Two ways to attach the phone audio:

1. SIP. Set `LIVEKIT_SIP_URI`. The carrier dials that URI instead of opening our media websocket. Tokens you can use: `{room}`, `{call_id}`, `{from_number}`, `{to_number}`.

   ```
   LIVEKIT_SIP_URI=sip:{room}@your-project.sip.livekit.cloud
   ```

   Match the LiveKit inbound trunk to the dialed user part, which is the room name.

2. In-process bridge. Leave `LIVEKIT_SIP_URI` empty and install `requirements-livekit.txt`. Agentline publishes the caller into the room and plays the agent audio back to the phone.

   Or bring your own bridge:

   ```python
   from agentline.voice.runtimes.livekit import register_livekit_bridge

   async def bridge(websocket, ctx):
       ...

   register_livekit_bridge(bridge)
   ```

### Pipecat

`VOICE_RUNTIME=pipecat`

Install `requirements-pipecat.txt` to use the default bot (Deepgram, OpenAI, Cartesia on the carrier websocket). Pipecat changes its pipeline classes often. When you already have a bot, point the hook at it:

```python
from agentline.voice.runtimes.pipecat import register_pipecat_factory

async def bot(websocket, ctx):
    # ctx.call_id, ctx.system_prompt, ctx.initial_greeting, ctx.voice_id, ctx.media
    ...

register_pipecat_factory(bot)
```

or

```
PIPECAT_FACTORY=myapp.bot:bot
```

`ctx.media` is `twilio`, `signalwire`, `telnyx`, or `plivo`. SignalWire and Telnyx TeXML use the same Media Streams JSON as Twilio, so a Twilio Pipecat serializer works for those three.

### Your own runtime

```python
from agentline.voice.runtime import AnswerPlan, register_voice_runtime

class MyRuntime:
    name = "mine"

    async def prepare(self, ctx):
        # "stream" opens our media websocket and then calls run().
        # "sip" dials sip_uri from the carrier.
        # "xml" returns xml as the carrier document.
        return AnswerPlan(mode="stream")

    async def run(self, websocket, ctx):
        ...

register_voice_runtime("mine", MyRuntime)
```

`VOICE_RUNTIME=mine`, or `VOICE_RUNTIME=myapp.runtime:MyRuntime`.
