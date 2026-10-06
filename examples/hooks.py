"""Register a carrier, a voice runtime, and a TTS vendor.

Import this module before the app starts serving, or set the env vars to
``module:Class`` paths and skip the register_* calls. See docs/providers.md.
"""


class AcmeCarrier:
    name = "acme"
    media = "twilio"

    def is_configured(self) -> bool:
        return True

    def stream_xml(self, call_id: str) -> str:
        from agentline.providers.base import LamlMedia
        return LamlMedia("acme", media=self.media).stream_xml(call_id)

    def sip_dial_xml(self, sip_uri: str, headers=None) -> str:
        from agentline.providers.base import LamlMedia
        return LamlMedia("acme").sip_dial_xml(sip_uri, headers)

    def say_xml(self, text: str) -> str:
        from agentline.providers.base import LamlMedia
        return LamlMedia("acme").say_xml(text)

    async def initiate_call(self, from_number, to_number, call_id) -> str:
        raise NotImplementedError("Call your carrier's create-call API here.")

    async def hangup_call(self, provider_call_id: str) -> None:
        raise NotImplementedError

    async def send_sms(self, from_number, to_number, body, media_url=None):
        from agentline.providers.base import SmsResult
        raise NotImplementedError

    async def provision_number(self, country="US", number_type="local", area_code=None, pattern=None, agent_id=None):
        from agentline.providers.base import ProvisionedNumber
        raise NotImplementedError

    async def release_number(self, provider_id: str) -> None:
        return None

    async def configure_number(self, provider_id: str) -> None:
        return None

    async def set_status_callback(self, provider_call_id: str, call_id: str) -> None:
        return None

    async def reconfigure_active_numbers(self) -> None:
        return None


class SilentTTS:
    """Stand-in synthesizer. Return 8 kHz mulaw bytes from your vendor."""

    name = "silent"

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        return b""


class EchoRuntime:
    name = "echo"

    async def prepare(self, ctx):
        from agentline.voice.runtime import AnswerPlan
        return AnswerPlan(mode="stream")

    async def run(self, websocket, ctx):
        return None


def install() -> None:
    from agentline.providers import register_telephony
    from agentline.voice.hooks import register_tts
    from agentline.voice.runtime import register_voice_runtime

    register_telephony("acme", AcmeCarrier)
    register_tts("silent", SilentTTS)
    register_voice_runtime("echo", EchoRuntime)


if __name__ == "__main__":
    install()
    print("hooks registered: acme, silent, echo")
