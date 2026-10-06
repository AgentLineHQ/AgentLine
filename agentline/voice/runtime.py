"""Voice runtime hooks.

A voice runtime owns the conversation once the carrier has audio. The
built-in runtime runs STT, LLM, and TTS inside this process. LiveKit and
Pipecat hand the call to those systems. Register another one the same way:

    from agentline.voice.runtime import register_voice_runtime
    register_voice_runtime("my-runtime", MyRuntime)

Select it with ``VOICE_RUNTIME``, or set ``agents.voice_runtime`` for one agent.
A runtime implements:

    async def prepare(self, ctx: CallContext) -> AnswerPlan
    async def run(self, websocket, ctx: CallContext) -> None
"""

import inspect
import logging
from dataclasses import dataclass, field

from agentline.config import settings
from agentline.providers.registry import load_object

logger = logging.getLogger(__name__)

BUILTIN_RUNTIMES = ("builtin", "livekit", "pipecat")

_factories: dict = {}


@dataclass
class CallContext:
    call_id: str
    system_prompt: str
    initial_greeting: str | None
    voice_id: str
    model_tier: str
    media: str
    from_number: str = ""
    to_number: str = ""
    direction: str = ""
    provider_call_id: str = ""
    voice_runtime: str | None = None
    agent_id: str | None = None
    account_id: str | None = None
    voicemail_message: str | None = None
    relay_mode: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class AnswerPlan:
    """How the carrier should attach media after the call is answered.

    ``stream`` connects the carrier websocket to ``run``.
    ``sip`` dials ``sip_uri`` and does not open our media websocket.
    ``xml`` returns ``xml`` unchanged.
    """

    mode: str = "stream"
    sip_uri: str | None = None
    xml: str | None = None


def register_voice_runtime(name: str, factory) -> None:
    _factories[name] = factory
    logger.info("Registered voice runtime '%s'", name)


def registered_runtimes() -> list[str]:
    names = list(BUILTIN_RUNTIMES)
    for name in _factories:
        if name not in names:
            names.append(name)
    return names


def _instantiate(factory):
    if isinstance(factory, type) or inspect.isfunction(factory):
        return factory()
    return factory


def _builtin(name: str):
    if name in ("builtin", "hosted", ""):
        from agentline.voice.runtimes.builtin import BuiltinRuntime
        return BuiltinRuntime()
    if name == "livekit":
        from agentline.voice.runtimes.livekit import LiveKitRuntime
        return LiveKitRuntime()
    if name == "pipecat":
        from agentline.voice.runtimes.pipecat import PipecatRuntime
        return PipecatRuntime()
    return None


def get_voice_runtime(name: str | None = None):
    """Return the runtime for ``name``, or ``VOICE_RUNTIME`` when name is empty."""
    key = (name or settings.VOICE_RUNTIME or "builtin").strip()
    if key in ("hosted", "default"):
        key = (settings.VOICE_RUNTIME or "builtin").strip()
    if key in _factories:
        return _instantiate(_factories[key])
    builtin = _builtin(key)
    if builtin is not None:
        return builtin
    if "." in key or ":" in key:
        return load_object(key)
    known = ", ".join(registered_runtimes())
    raise RuntimeError(
        f"Unknown voice runtime '{key}'. Use one of: {known}. "
        "Or set VOICE_RUNTIME to module:Class."
    )
