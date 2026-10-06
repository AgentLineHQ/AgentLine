"""Pipecat voice runtime.

Set ``VOICE_RUNTIME=pipecat`` to run a Pipecat pipeline on the carrier
websocket. The default pipeline uses Deepgram, an OpenAI-compatible model,
and Cartesia when ``pipecat-ai`` is installed:

    pip install -r requirements-pipecat.txt

Replace that pipeline with your own bot:

    from agentline.voice.runtimes.pipecat import register_pipecat_factory

    async def bot(websocket, ctx):
        ...

    register_pipecat_factory(bot)

or set ``PIPECAT_FACTORY=myapp.bot:bot``. The factory is
``async def bot(websocket, ctx)``.
"""

import logging

from agentline.config import settings
from agentline.providers.registry import load_attr
from agentline.voice.runtime import AnswerPlan, CallContext

logger = logging.getLogger(__name__)

_factory = None


def register_pipecat_factory(factory) -> None:
    """Register ``async def factory(websocket, ctx)`` as the Pipecat bot."""
    global _factory
    _factory = factory
    logger.info("Registered Pipecat factory")


def _resolve_factory():
    if _factory is not None:
        return _factory
    if settings.PIPECAT_FACTORY:
        factory = load_attr(settings.PIPECAT_FACTORY)
        if isinstance(factory, type):
            factory = factory()
        return factory
    return default_pipecat_bot


class PipecatRuntime:
    name = "pipecat"

    async def prepare(self, ctx: CallContext) -> AnswerPlan:
        factory = _resolve_factory()
        if factory is default_pipecat_bot:
            try:
                import pipecat  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "Pipecat is not installed. pip install -r requirements-pipecat.txt, "
                    "or set PIPECAT_FACTORY / register_pipecat_factory()."
                ) from exc
        return AnswerPlan(mode="stream")

    async def run(self, websocket, ctx: CallContext) -> None:
        factory = _resolve_factory()
        await factory(websocket, ctx)


async def default_pipecat_bot(websocket, ctx: CallContext) -> None:
    """A small Pipecat pipeline wired to the same keys as the built-in runtime.

    Pipecat's own APIs move quickly. If this default fails to import, point
    ``PIPECAT_FACTORY`` at the bot you already run.
    """
    try:
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.runner.types import WebSocketRunnerArguments
        from pipecat.runner.utils import create_transport
        from pipecat.services.cartesia.tts import CartesiaTTSService
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.services.openai.llm import OpenAILLMService
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
        from pipecat.workers.runner import WorkerRunner
    except ImportError as exc:
        raise RuntimeError(
            "The default Pipecat bot could not import pipecat. "
            "Install requirements-pipecat.txt or set PIPECAT_FACTORY to your bot."
        ) from exc

    transport_params = {
        "twilio": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
        "plivo": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
        "telnyx": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    runner_args = WebSocketRunnerArguments(websocket=websocket)
    transport = await create_transport(runner_args, transport_params)

    prompt = ctx.system_prompt or "You are a helpful voice assistant. Keep responses brief."
    if ctx.initial_greeting:
        prompt = f"{prompt}\n\nBegin the call by saying exactly: {ctx.initial_greeting}"

    llm_settings = getattr(OpenAILLMService, "Settings", None)
    if llm_settings is not None:
        llm = OpenAILLMService(
            api_key=settings.OPENAI_API_KEY,
            settings=llm_settings(system_instruction=prompt),
        )
    else:
        llm = OpenAILLMService(api_key=settings.OPENAI_API_KEY, system_instruction=prompt)

    stt = DeepgramSTTService(api_key=settings.DEEPGRAM_API_KEY)
    tts_settings = getattr(CartesiaTTSService, "Settings", None)
    if tts_settings is not None and ctx.voice_id:
        tts = CartesiaTTSService(
            api_key=settings.CARTESIA_API_KEY,
            settings=tts_settings(voice=ctx.voice_id),
        )
    else:
        tts = CartesiaTTSService(api_key=settings.CARTESIA_API_KEY)

    user_params = LLMUserAggregatorParams()
    try:
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        user_params = LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    except Exception:
        logger.info("Pipecat Silero VAD is unavailable — continuing without it.")

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=user_params,
    )
    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
    ])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
        ),
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        await runner.cancel()

    await runner.run()
