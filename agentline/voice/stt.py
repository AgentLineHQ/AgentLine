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
