"""
Regression tests for Owner Task Mode.

These pin down the v1.08 behaviour: owner task mode must trigger in BOTH
directions (inbound AND outbound) when the owner's number is involved, and
the OWNER_MODE_SENTINEL prefix must be present so the hangup handler emits
`call.owner_task` events instead of plain `call.completed`.

Pure unit tests — no database, no server, no network. Runs with stdlib only:

    python test_owner_mode.py

This guards against the original bug where the outbound path in
routers/calls.py silently fell back to the default support greeting
because it never compared to_number against agent.owner_phone.
"""

import unittest

from agentline.voice.owner_mode import (
    OWNER_MODE_GREETING,
    OWNER_MODE_SENTINEL,
    build_owner_prompt,
    is_owner_number,
    resolve_outbound_owner_overrides,
)


def _agent(owner_phone=None, system_prompt="You are a support agent."):
    """Stand-in for an asyncpg.Record / dict agent row."""
    return {
        "id": "agt_test",
        "system_prompt": system_prompt,
        "owner_phone": owner_phone,
        "initial_greeting": "Hello, how can I help you today?",
    }


class TestIsOwnerNumber(unittest.TestCase):
    def test_matches_owner_phone_exact(self):
        self.assertTrue(is_owner_number(_agent(owner_phone="+12125551234"), "+12125551234"))

    def test_matches_without_plus_on_either_side(self):
        # owner stored without '+', caller id has '+'
        self.assertTrue(is_owner_number(_agent(owner_phone="12125551234"), "+12125551234"))
        # owner stored with '+', caller id without
        self.assertTrue(is_owner_number(_agent(owner_phone="+12125551234"), "12125551234"))

    def test_does_not_match_other_number(self):
        self.assertFalse(is_owner_number(_agent(owner_phone="+12125551234"), "+18005551234"))

    def test_no_owner_phone_registered(self):
        self.assertFalse(is_owner_number(_agent(owner_phone=None), "+12125551234"))
        self.assertFalse(is_owner_number(_agent(owner_phone=""), "+12125551234"))

    def test_no_agent(self):
        self.assertFalse(is_owner_number(None, "+12125551234"))

    def test_empty_number(self):
        self.assertFalse(is_owner_number(_agent(owner_phone="+12125551234"), ""))
        self.assertFalse(is_owner_number(_agent(owner_phone="+12125551234"), None))


class TestBuildOwnerPrompt(unittest.TestCase):
    def test_starts_with_sentinel(self):
        # The hangup handler keys off this prefix to emit call.owner_task.
        prompt = build_owner_prompt(_agent(system_prompt="base personality"))
        self.assertTrue(prompt.startswith(OWNER_MODE_SENTINEL))

    def test_includes_agent_base_prompt(self):
        prompt = build_owner_prompt(_agent(system_prompt="PERSONALITY: upbeat."))
        self.assertIn("PERSONALITY: upbeat.", prompt)

    def test_handles_missing_system_prompt(self):
        prompt = build_owner_prompt({"owner_phone": "+12125551234"})
        self.assertTrue(prompt.startswith(OWNER_MODE_SENTINEL))

    def test_handles_no_agent(self):
        prompt = build_owner_prompt(None)
        self.assertTrue(prompt.startswith(OWNER_MODE_SENTINEL))

    def test_capability_boundary_is_present(self):
        # The voice AI must know it cannot execute live — it only records.
        prompt = build_owner_prompt(_agent())
        self.assertIn("CANNOT execute", prompt)
        self.assertIn("AFTER you hang up", prompt)


class TestResolveOutboundOwnerOverrides(unittest.TestCase):
    """The core of the v1.08 fix — outbound owner detection."""

    def test_owner_destination_gets_owner_prompt_and_greeting(self):
        agent = _agent(owner_phone="+12125551234")
        sp, greet, is_owner = resolve_outbound_owner_overrides(
            agent, "+12125551234", body_system_prompt=None, body_initial_greeting=None,
        )
        self.assertTrue(is_owner)
        self.assertTrue(sp.startswith(OWNER_MODE_SENTINEL))
        self.assertEqual(greet, OWNER_MODE_GREETING)

    def test_non_owner_destination_uses_agent_default(self):
        agent = _agent(owner_phone="+12125551234", system_prompt="default prompt")
        sp, greet, is_owner = resolve_outbound_owner_overrides(
            agent, "+18005559999", body_system_prompt=None, body_initial_greeting=None,
        )
        self.assertFalse(is_owner)
        self.assertEqual(sp, "default prompt")
        # greeting left None on purpose — WebSocket stream handler fills it.
        self.assertIsNone(greet)

    def test_explicit_body_override_beats_owner_mode(self):
        # Per-call overrides are the highest priority tier — even when
        # dialling the owner, an explicit override must be honoured.
        agent = _agent(owner_phone="+12125551234")
        sp, greet, is_owner = resolve_outbound_owner_overrides(
            agent, "+12125551234",
            body_system_prompt="custom one-shot prompt",
            body_initial_greeting="custom opener",
        )
        # Still flagged as owner (for logging / event semantics) but the
        # caller's explicit values win.
        self.assertTrue(is_owner)
        self.assertEqual(sp, "custom one-shot prompt")
        self.assertEqual(greet, "custom opener")

    def test_owner_number_without_plus_prefix_still_detected(self):
        agent = _agent(owner_phone="12125551234")  # stored without +
        sp, greet, is_owner = resolve_outbound_owner_overrides(
            agent, "+12125551234", None, None,  # dialled with +
        )
        self.assertTrue(is_owner)
        self.assertEqual(greet, OWNER_MODE_GREETING)

    def test_no_owner_phone_registered_is_safe(self):
        agent = _agent(owner_phone=None)
        sp, greet, is_owner = resolve_outbound_owner_overrides(
            agent, "+12125551234", None, None,
        )
        self.assertFalse(is_owner)
        self.assertEqual(sp, agent["system_prompt"])

    def test_sentinel_survives_into_stored_prompt(self):
        # Regression for the original bug: the calls.system_prompt written
        # by create_call MUST carry the sentinel, otherwise the hangup
        # handler (signalwire_events.py) classifies it as a normal call
        # and never emits call.owner_task.
        agent = _agent(owner_phone="+12125551234")
        sp, _, _ = resolve_outbound_owner_overrides(agent, "+12125551234", None, None)
        self.assertTrue(sp.startswith(OWNER_MODE_SENTINEL))


class TestSentinelConsistency(unittest.TestCase):
    def test_sentinel_matches_first_line_of_prompt(self):
        # build_owner_prompt output must begin with the exact sentinel
        # string the hangup handler searches for. If this breaks, owner
        # calls silently downgrade to call.completed again.
        from agentline.voice import owner_mode
        first_line = owner_mode.OWNER_MODE_PROMPT.splitlines()[0]
        self.assertTrue(
            first_line.startswith(OWNER_MODE_SENTINEL),
            f"prompt first line {first_line!r} does not start with sentinel {OWNER_MODE_SENTINEL!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
