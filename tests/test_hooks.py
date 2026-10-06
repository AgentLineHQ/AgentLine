"""Provider and voice-runtime hooks resolve without a database or carrier."""

import unittest

from agentline.providers.base import LamlMedia
from agentline.providers.registry import get_provider, register_telephony
from agentline.voice.hooks import get_tts, register_tts
from agentline.voice.runtime import get_voice_runtime, register_voice_runtime
from agentline.voice.runtimes.livekit import format_sip_uri
from agentline.voice.runtime import AnswerPlan, CallContext
from agentline.config import settings


class _FakeTTS:
    name = "fake-tts"

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        return b"\xff"


class _FakeRuntime:
    name = "fake-runtime"

    async def prepare(self, ctx):
        return AnswerPlan(mode="stream")

    async def run(self, websocket, ctx):
        return None


class _FakeCarrier:
    name = "fake-carrier"
    media = "twilio"

    def is_configured(self):
        return True


class HookTests(unittest.TestCase):
    def test_laml_stream_points_at_provider_path(self):
        xml = LamlMedia("signalwire").stream_xml("call_123")
        self.assertIn("/signalwire/stream/call_123", xml)
        self.assertIn("<Connect>", xml)
        self.assertIn("<Stream", xml)

    def test_laml_sip_includes_header(self):
        xml = LamlMedia("twilio").sip_dial_xml(
            "sip:room@example.com",
            {"X-Agentline-Call-Id": "call_123"},
        )
        self.assertIn("sip:room@example.com", xml)
        self.assertIn("X-Agentline-Call-Id", xml)
        self.assertIn("call_123", xml)

    def test_builtin_providers_construct(self):
        for name in ("signalwire", "twilio", "plivo", "telnyx"):
            provider = get_provider(name)
            self.assertEqual(provider.name, name)
            xml = provider.stream_xml("call_abc")
            self.assertIn("/" + name + "/stream/call_abc", xml)
            self.assertIn("Response", provider.say_xml("hi"))

    def test_registered_carrier_wins(self):
        register_telephony("fake-carrier", _FakeCarrier)
        provider = get_provider("fake-carrier")
        self.assertEqual(provider.name, "fake-carrier")

    def test_unknown_carrier_raises(self):
        with self.assertRaises(RuntimeError):
            get_provider("not-a-carrier")

    def test_tts_registration(self):
        register_tts("fake-tts", _FakeTTS)
        previous = settings.TTS_PROVIDER
        settings.TTS_PROVIDER = "fake-tts"
        try:
            self.assertEqual(get_tts().name, "fake-tts")
        finally:
            settings.TTS_PROVIDER = previous

    def test_runtime_registration_and_builtin(self):
        runtime = get_voice_runtime("builtin")
        self.assertEqual(runtime.name, "builtin")
        register_voice_runtime("fake-runtime", _FakeRuntime)
        self.assertEqual(get_voice_runtime("fake-runtime").name, "fake-runtime")

    def test_sip_uri_tokens(self):
        ctx = CallContext(
            call_id="call_1",
            system_prompt="",
            initial_greeting=None,
            voice_id="female-1",
            model_tier="balanced",
            media="twilio",
            from_number="+15550001",
            to_number="+15550002",
        )
        rendered = format_sip_uri("sip:{room}@sip.example?c={call_id}", ctx, "call-call_1")
        self.assertEqual(rendered, "sip:call-call_1@sip.example?c=call_1")

    def test_settings_have_no_supabase_requirement(self):
        self.assertTrue(hasattr(settings, "TELEPHONY_PROVIDER"))
        self.assertTrue(hasattr(settings, "VOICE_RUNTIME"))
        self.assertFalse(hasattr(settings, "SUPABASE_URL"))


if __name__ == "__main__":
    unittest.main()
