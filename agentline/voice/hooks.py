"""STT, TTS, and LLM hooks used by the built-in voice runtime.

Register a replacement, then select it with an environment variable:

    from agentline.voice.hooks import register_tts
    register_tts("elevenlabs", ElevenLabsTTS)
    # TTS_PROVIDER=elevenlabs

Or skip registration and point the variable at your class:

    TTS_PROVIDER=myapp.voices:ElevenLabsTTS

A TTS object implements ``async synthesize(text, voice_id) -> bytes`` of
8 kHz mulaw. An LLM object implements ``stream(system_prompt, history,
model_tier)`` and yields sentence-sized strings. An STT object implements
``open()`` and returns a session with ``on_transcript``, ``on_utterance_end``,
``start``, ``send``, and ``finish``.
"""

import inspect
import logging

from agentline.config import settings
from agentline.providers.registry import load_object

logger = logging.getLogger(__name__)

BUILTIN_STT = ("deepgram",)
BUILTIN_TTS = ("cartesia",)
BUILTIN_LLM = ("openai",)

_stt: dict = {}
_tts: dict = {}
_llm: dict = {}


def register_stt(name: str, factory) -> None:
    _stt[name] = factory
    logger.info("Registered STT provider '%s'", name)


def register_tts(name: str, factory) -> None:
    _tts[name] = factory
    logger.info("Registered TTS provider '%s'", name)


def register_llm(name: str, factory) -> None:
    _llm[name] = factory
    logger.info("Registered LLM provider '%s'", name)


def _instantiate(factory):
    if isinstance(factory, type) or inspect.isfunction(factory):
        return factory()
    return factory


def _resolve(kind: str, key: str, registry: dict):
    key = (key or "").strip()
    if key in registry:
        return _instantiate(registry[key])
    if "." in key or ":" in key:
        return load_object(key)
    if kind == "stt" and key in ("", "deepgram"):
        from agentline.voice.stt import DeepgramSTT
        return DeepgramSTT()
    if kind == "tts" and key in ("", "cartesia"):
        from agentline.voice.tts import CartesiaTTS
        return CartesiaTTS()
    if kind == "llm" and key in ("", "openai"):
        from agentline.voice.llm import OpenAILLM
        return OpenAILLM()
    known = sorted(set(list(registry) + list(_builtins(kind))))
    raise RuntimeError(
        f"Unknown {kind} provider '{key}'. Use one of: {', '.join(known)}. "
        f"Or set the provider env var to module:Class."
    )


def _builtins(kind: str) -> tuple[str, ...]:
    if kind == "stt":
        return BUILTIN_STT
    if kind == "tts":
        return BUILTIN_TTS
    return BUILTIN_LLM


def get_stt():
    return _resolve("stt", settings.STT_PROVIDER, _stt)


def get_tts():
    return _resolve("tts", settings.TTS_PROVIDER, _tts)


def get_llm():
    return _resolve("llm", settings.LLM_PROVIDER, _llm)


def registered_stt() -> list[str]:
    return list(dict.fromkeys([*BUILTIN_STT, *_stt]))


def registered_tts() -> list[str]:
    return list(dict.fromkeys([*BUILTIN_TTS, *_tts]))


def registered_llm() -> list[str]:
    return list(dict.fromkeys([*BUILTIN_LLM, *_llm]))
