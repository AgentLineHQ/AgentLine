"""In-process voice runtime.

Runs the STT → LLM → TTS pipeline in this process. Which vendors those
steps call is decided by the STT, TTS, and LLM hooks.
"""

from agentline.voice.pipeline import run_pipeline
from agentline.voice.runtime import AnswerPlan, CallContext


class BuiltinRuntime:
    name = "builtin"

    async def prepare(self, ctx: CallContext) -> AnswerPlan:
        return AnswerPlan(mode="stream")

    async def run(self, websocket, ctx: CallContext) -> None:
        await run_pipeline(
            provider_ws=websocket,
            call_id=ctx.call_id,
            system_prompt=ctx.system_prompt,
            initial_greeting=ctx.initial_greeting,
            voice_id=ctx.voice_id,
            model_tier=ctx.model_tier,
            provider=ctx.media,
            call_direction=ctx.direction or "inbound",
            voicemail_message=ctx.voicemail_message,
            agent_id=ctx.agent_id,
            account_id=ctx.account_id,
            relay_mode=ctx.relay_mode,
        )
