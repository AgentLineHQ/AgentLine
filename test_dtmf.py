"""
Regression tests for DTMF audio + outbound IVR / voicemail policy.

Pins the failure from a live call where the agent pressed 0 four times
on a mailbox that said "To disconnect, press 1. To record your message,
press 2." Pure unit tests — no network:

    python test_dtmf.py
"""

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agentline.voice.dtmf import (
    GAP_MS,
    SAMPLE_RATE,
    TONE_MS,
    W_PAUSE_MS,
    generate_dtmf,
)
from agentline.voice.ivr import (
    build_outbound_turn_hint,
    classify_outbound_utterance,
    extract_menu_options,
    is_live_greeting,
    resolve_outbound_action,
    tone_digits,
)


RECORD_MENU = (
    "We didn't get your message either because you were not speaking "
    "or because of a bad connection. To disconnect, press 1. "
    "To record your message, press 2."
)


class TestGenerateDtmf(unittest.TestCase):
    def test_empty_is_silent(self):
        self.assertEqual(generate_dtmf(""), b"")
        self.assertEqual(generate_dtmf(None or ""), b"")

    def test_single_digit_length(self):
        audio = generate_dtmf("0")
        expected = int(SAMPLE_RATE * (TONE_MS + GAP_MS) / 1000)
        self.assertEqual(len(audio), expected)

    def test_w_is_half_second_pause(self):
        audio = generate_dtmf("w")
        self.assertEqual(len(audio), int(SAMPLE_RATE * W_PAUSE_MS / 1000))
        self.assertTrue(set(audio) <= {0xFF})

    def test_unknown_chars_skipped(self):
        self.assertEqual(generate_dtmf("xyz"), b"")
        self.assertEqual(generate_dtmf("1x2"), generate_dtmf("12"))


class TestClassifyAndExtract(unittest.TestCase):
    def test_number_not_available_is_voicemail_greeting(self):
        self.assertEqual(
            classify_outbound_utterance("12418174 is not available."),
            "voicemail_greeting",
        )

    def test_classic_beep_greeting(self):
        self.assertEqual(
            classify_outbound_utterance(
                "You've reached Bob. Please leave a message after the beep."
            ),
            "voicemail_greeting",
        )

    def test_record_menu_from_transcript(self):
        self.assertEqual(
            classify_outbound_utterance(RECORD_MENU),
            "voicemail_record_menu",
        )

    def test_live_human_not_available_is_not_voicemail(self):
        self.assertEqual(
            classify_outbound_utterance(
                "Sorry, she's not available right now, can I take a message?"
            ),
            "live",
        )

    def test_first_person_not_available_is_live(self):
        self.assertEqual(
            classify_outbound_utterance("I'm not available on Tuesday."),
            "live",
        )

    def test_shes_not_available_without_mailbox_language_is_live(self):
        # Receptionist, not a carrier mailbox. Do not hang up and leave VM.
        self.assertEqual(
            classify_outbound_utterance("She's not available right now."),
            "live",
        )

    def test_receptionist_offer_is_live(self):
        self.assertEqual(
            classify_outbound_utterance(
                "The doctor is not available until Thursday, would you like a callback?"
            ),
            "live",
        )

    def test_sales_ivr(self):
        self.assertEqual(
            classify_outbound_utterance(
                "Press 1 for sales. Press 2 for support. Dial 0 for the operator."
            ),
            "ivr",
        )

    def test_hello_is_live(self):
        self.assertEqual(classify_outbound_utterance("Hello?"), "live")

    def test_only_short_greetings_fast_path_the_introduction(self):
        for utterance in ("Hello?", "Hi", "yeah!", "Yes."):
            with self.subTest(utterance=utterance):
                self.assertTrue(is_live_greeting(utterance))

        for utterance in (
            "Hello, you've reached Bob. Leave a message after the beep.",
            "Press 1 for sales.",
            "Please state your name and why you're calling.",
        ):
            with self.subTest(utterance=utterance):
                self.assertFalse(is_live_greeting(utterance))

    def test_extract_record_menu_labels(self):
        opts = extract_menu_options(RECORD_MENU)
        by_digit = {opt.digit: opt.label for opt in opts}
        self.assertEqual(by_digit.get("1"), "disconnect")
        self.assertEqual(by_digit.get("2"), "record")
        self.assertNotIn("0", by_digit)

    def test_extract_word_digits(self):
        opts = extract_menu_options("Press one for sales, or press star to repeat.")
        digits = [opt.digit for opt in opts]
        self.assertEqual(digits, ["1", "*"])


class TestResolveTranscriptFailure(unittest.TestCase):
    """The exact call that kept pressing 0."""

    def test_not_available_overrides_dtmf_zero(self):
        action = resolve_outbound_action(
            "12418174 is not available.",
            llm_digits="0",
            last_dtmf=None,
            has_voicemail_message=True,
        )
        self.assertEqual(action.kind, "voicemail")
        self.assertIsNone(action.digits)

    def test_not_available_forced_before_llm(self):
        action = resolve_outbound_action(
            "12418174 is not available.",
            llm_digits=None,
            llm_voicemail=False,
            has_voicemail_message=True,
        )
        self.assertEqual(action.kind, "voicemail")

    def test_record_menu_zero_becomes_two(self):
        action = resolve_outbound_action(
            RECORD_MENU,
            llm_digits="0",
            last_dtmf="0",
            has_voicemail_message=True,
        )
        self.assertEqual(action.kind, "dtmf")
        self.assertEqual(tone_digits(action.digits), "2")
        self.assertTrue(action.then_voicemail)

    def test_record_menu_forced_before_llm(self):
        action = resolve_outbound_action(
            RECORD_MENU,
            llm_digits=None,
            has_voicemail_message=True,
        )
        self.assertEqual(action.kind, "dtmf")
        self.assertEqual(action.digits, "2")
        self.assertTrue(action.then_voicemail)

    def test_record_menu_without_voicemail_message_disconnects(self):
        action = resolve_outbound_action(
            RECORD_MENU,
            llm_digits="0",
            has_voicemail_message=False,
        )
        self.assertEqual(action.kind, "dtmf")
        self.assertEqual(action.digits, "1")
        self.assertFalse(action.then_voicemail)

    def test_repeating_zero_four_times_never_stays_on_zero(self):
        last = None
        for _ in range(4):
            action = resolve_outbound_action(
                RECORD_MENU,
                llm_digits="0",
                last_dtmf=last,
                has_voicemail_message=True,
            )
            self.assertNotEqual(tone_digits(action.digits), "0")
            last = action.digits


class TestResolveIvr(unittest.TestCase):
    MENU = "Press 1 for sales. Press 2 for support. Dial 0 for the operator."

    def test_valid_llm_digit_kept(self):
        action = resolve_outbound_action(
            self.MENU, llm_digits="2", last_dtmf=None
        )
        self.assertEqual(action.kind, "dtmf")
        self.assertEqual(action.digits, "2")
        self.assertEqual(action.reason, "ivr_llm_valid")

    def test_unlisted_digit_corrected(self):
        action = resolve_outbound_action(
            "Press 1 for sales. Press 2 for support.",
            llm_digits="9",
            last_dtmf=None,
        )
        self.assertEqual(action.kind, "dtmf")
        self.assertIn(action.digits, {"1", "2"})

    def test_repeat_same_digit_picks_another(self):
        action = resolve_outbound_action(
            self.MENU, llm_digits="1", last_dtmf="1"
        )
        self.assertEqual(action.kind, "dtmf")
        self.assertNotEqual(tone_digits(action.digits), "1")

    def test_await_llm_on_first_ivr_pass(self):
        action = resolve_outbound_action(self.MENU, llm_digits=None)
        self.assertEqual(action.kind, "passthrough")

    def test_speech_option_not_forced(self):
        action = resolve_outbound_action(
            "Say yes or press 1 to accept the call.",
            llm_digits=None,
        )
        self.assertEqual(action.kind, "passthrough")

    def test_live_hello_does_not_invent_dtmf(self):
        action = resolve_outbound_action("Hello?", llm_digits=None)
        self.assertEqual(action.kind, "passthrough")

    def test_dtmf_on_hello_is_dropped(self):
        action = resolve_outbound_action("Hello?", llm_digits="0")
        self.assertEqual(action.kind, "drop")

    def test_zero_only_when_listed(self):
        action = resolve_outbound_action(
            "Press 1 for sales. Press 2 for support.",
            llm_digits="0",
        )
        self.assertNotEqual(tone_digits(action.digits), "0")


class TestOutboundPrompt(unittest.TestCase):
    """Read the prompt from source so we don't import the Deepgram-heavy pipeline."""

    @classmethod
    def setUpClass(cls):
        cls.src = (
            Path(__file__).resolve().parent / "agentline" / "voice" / "pipeline.py"
        ).read_text(encoding="utf-8")

    def test_does_not_advertise_zero_as_default(self):
        self.assertNotIn("if nothing fits", self.src.lower())
        self.assertIn("0 is not a default", self.src)

    def test_teaches_not_available_as_voicemail(self):
        self.assertIn("12418174 is not available", self.src)
        self.assertIn("VOICEMAIL_DETECTED", self.src)

    def test_teaches_record_menu(self):
        self.assertIn("To record your message, press 2", self.src)

    def test_live_greeting_uses_configured_introduction_without_llm(self):
        self.assertIn("use_outbound_introduction", self.src)
        self.assertIn("_outbound_introduction_generate", self.src)


class TestCartesiaPrewarm(unittest.IsolatedAsyncioTestCase):
    async def test_prewarm_only_opens_connection(self):
        fake_cartesia = SimpleNamespace(AsyncCartesia=object)
        tts_was_loaded = "agentline.voice.tts" in sys.modules
        with patch.dict(sys.modules, {"cartesia": fake_cartesia}):
            tts = importlib.import_module("agentline.voice.tts")

        try:
            get_connection = AsyncMock(return_value=object())
            with patch.object(tts, "_get_ws_connection", get_connection):
                await tts.prewarm_cartesia_connection()

            get_connection.assert_awaited_once_with()
        finally:
            if not tts_was_loaded:
                sys.modules.pop("agentline.voice.tts", None)


class TestTurnHint(unittest.TestCase):
    def test_greeting_hint_forbids_dtmf(self):
        hint = build_outbound_turn_hint(
            "12418174 is not available.", last_dtmf=None, has_voicemail_message=True
        )
        self.assertIn("VOICEMAIL_DETECTED", hint)
        self.assertIn("Do not press", hint)

    def test_record_menu_hint_lists_keys(self):
        hint = build_outbound_turn_hint(
            RECORD_MENU, last_dtmf="0", has_voicemail_message=True
        )
        self.assertIn("1=disconnect", hint)
        self.assertIn("2=record", hint)
        self.assertIn("last press (0)", hint.lower())


class TestCallDtmfStateScoping(unittest.TestCase):
    """The production silence bug: assigning last_dtmf_digits in a nested
    function without nonlocal made every outbound 'Hello?' crash before TTS.
    Mutating an object attribute does not have that problem.
    """

    def test_bare_assignment_shadows_and_crashes_on_read(self):
        last_dtmf_digits = None

        def handle(outbound: bool) -> str | None:
            if outbound:
                return last_dtmf_digits
            last_dtmf_digits = "1"
            return last_dtmf_digits

        with self.assertRaises(UnboundLocalError):
            handle(True)

    def test_object_attribute_is_safe_to_read_and_write(self):
        class State:
            def __init__(self):
                self.last_digits = None

        dtmf = State()

        def handle(outbound: bool) -> str | None:
            if outbound:
                return dtmf.last_digits
            dtmf.last_digits = "1"
            return dtmf.last_digits

        self.assertIsNone(handle(True))
        self.assertEqual(handle(False), "1")
        self.assertEqual(handle(True), "1")


if __name__ == "__main__":
    unittest.main()
