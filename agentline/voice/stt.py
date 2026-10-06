"""
AgentLine — Deepgram Streaming STT
Real-time speech-to-text using Deepgram's Nova-2 model.
"""

import logging
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from agentline.config import settings

logger = logging.getLogger(__name__)


def create_deepgram_connection():
    """Create a new Deepgram live transcription connection.

    Uses asyncwebsocket (current API) instead of deprecated asynclive.
    """
    client = DeepgramClient(settings.DEEPGRAM_API_KEY)
    return client.listen.asyncwebsocket.v("1")


class DeepgramSTT:
    """Streaming STT session factory. Selected when ``STT_PROVIDER=deepgram``."""

    name = "deepgram"

    def open(self) -> "DeepgramSession":
        return DeepgramSession()


class DeepgramSession:
    """One Deepgram live socket for a single call."""

    def __init__(self):
        self._conn = create_deepgram_connection()
        self._on_transcript = None
        self._on_utterance_end = None

    def on_transcript(self, callback) -> None:
        """``callback(text, speech_final)`` runs for each finalized segment."""
        self._on_transcript = callback

    def on_utterance_end(self, callback) -> None:
        self._on_utterance_end = callback

    async def start(self) -> None:
        async def _transcript(_self, result, **kwargs):
            if not result.is_final:
                return
            sentence = ""
            try:
                sentence = result.channel.alternatives[0].transcript or ""
            except Exception:
                sentence = ""
            if self._on_transcript:
                await self._on_transcript(sentence, bool(result.speech_final))

        async def _utterance_end(_self, utterance_end, **kwargs):
            if self._on_utterance_end:
                await self._on_utterance_end()

        self._conn.on(LiveTranscriptionEvents.Transcript, _transcript)
        self._conn.on(LiveTranscriptionEvents.UtteranceEnd, _utterance_end)
        await self._conn.start(get_stt_options())

    async def send(self, audio: bytes) -> None:
        await self._conn.send(audio)

    async def finish(self) -> None:
        await self._conn.finish()


def get_stt_options() -> LiveOptions:
    """Return the STT options optimized for phone calls.

    interim_results must be True for speech_final to work.
    The is_final guard in pipeline.py prevents duplicate buffering.
    """
    return LiveOptions(
        model="nova-2-phonecall",
        language="en-US",
        smart_format=True,
        interim_results=True,
        utterance_end_ms=1500,
        endpointing=800,
        vad_events=True,
        encoding="mulaw",
        sample_rate=8000,
    )
