import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from agentline.config import Settings
from agentline.voice.relay_context import (
    extract_context,
    queue_relay_acknowledgement,
    queue_relay_speech,
    speech_segments,
)
from agentline.voice.relay_store import deliver_turn_context, wait_for_turn_context
from agentline.routers.discovery import agentline_integration_discovery


class FakeRelayDB:
    def __init__(self, row):
        self.row = row
        self.updated = False

    async def fetchrow(self, _query, call_id, turn_id):
        if call_id != "call_1" or turn_id != "turn_1":
            return None
        return self.row

    async def fetchval(self, _query, call_id, turn_id, context):
        if self.row["state"] != "waiting":
            return None
        self.updated = True
        self.row["state"] = "answered"
        self.row["context"] = context
        return turn_id

    async def execute(self, *_args):
        return None


class FakeRelayWaitDB:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, *_args):
        return self.row

    async def fetchval(self, *_args):
        self.row["state"] = "consumed"
        return self.row["context"]


class RelayProtocolTests(unittest.IsolatedAsyncioTestCase):
    def _row(self, **overrides):
        row = {
            "account_id": "acct_1",
            "agent_id": "agt_1",
            "push_token_hash": "unused",
            "state": "waiting",
            "context": None,
            "call_status": "in-progress",
            "ended_at": None,
        }
        row.update(overrides)
        return row

    async def test_context_is_bound_to_exact_turn(self):
        db = FakeRelayDB(self._row())
        missing = await deliver_turn_context(
            db,
            call_id="call_1",
            turn_id="turn_2",
            context="wrong turn",
            account_id="acct_1",
        )
        self.assertEqual(missing, "not_found")
        self.assertFalse(db.updated)

        accepted = await deliver_turn_context(
            db,
            call_id="call_1",
            turn_id="turn_1",
            context="correct turn",
            account_id="acct_1",
        )
        self.assertEqual(accepted, "live")
        self.assertTrue(db.updated)

    async def test_cancelled_turn_rejects_late_context(self):
        db = FakeRelayDB(self._row(state="cancelled"))
        status = await deliver_turn_context(
            db,
            call_id="call_1",
            turn_id="turn_1",
            context="late result",
            account_id="acct_1",
        )
        self.assertEqual(status, "stale")
        self.assertFalse(db.updated)

    async def test_terminal_turn_stops_context_wait_immediately(self):
        for row in (
            None,
            {"state": "cancelled", "context": None},
            {"state": "expired", "context": None},
            {"state": "consumed", "context": "already handled"},
        ):
            db = FakeRelayWaitDB(row)

            @asynccontextmanager
            async def fake_db_conn():
                yield db

            with self.subTest(row=row), patch(
                "agentline.voice.relay_store.get_db_conn", fake_db_conn
            ):
                context, terminal = await wait_for_turn_context(
                    "call_1", "turn_1", timeout=60
                )

            self.assertIsNone(context)
            self.assertTrue(terminal)

    async def test_context_wait_distinguishes_answer_from_timeout(self):
        db = FakeRelayWaitDB({"state": "answered", "context": "Email sent."})

        @asynccontextmanager
        async def fake_db_conn():
            yield db

        with patch("agentline.voice.relay_store.get_db_conn", fake_db_conn):
            context, terminal = await wait_for_turn_context(
                "call_1", "turn_1", timeout=60
            )

        self.assertEqual(context, "Email sent.")
        self.assertFalse(terminal)

        context, terminal = await wait_for_turn_context(
            "call_1", "turn_1", timeout=0
        )
        self.assertIsNone(context)
        self.assertFalse(terminal)

    def test_acknowledgement_message_is_not_context(self):
        self.assertIsNone(extract_context({"message": "accepted"}))
        self.assertEqual(extract_context({"message": "The balance is $12"}), "The balance is $12")
        self.assertEqual(extract_context({"context": " balance is $12 "}), "balance is $12")

    async def test_discovery_explains_runtime_selection(self):
        manifest = await agentline_integration_discovery()
        self.assertEqual(manifest["recommended_transport"], "websocket")
        self.assertIn("hermes", manifest["runtime_selection"])
        self.assertIn("openclaw", manifest["runtime_selection"])
        self.assertEqual(manifest["context_response"]["correlation"], "turn_id")

    def test_websocket_url_derivation(self):
        self.assertEqual(
            Settings(BASE_URL="https://api.example.test/").ws_base_url,
            "wss://api.example.test",
        )
        self.assertEqual(
            Settings(BASE_URL="http://localhost:8000").ws_base_url,
            "ws://localhost:8000",
        )

    def test_direct_speech_is_split_without_rewriting(self):
        self.assertEqual(
            speech_segments("No new emails arrived. Your inbox is up to date."),
            ["No new emails arrived.", "Your inbox is up to date."],
        )

    async def test_direct_speech_streams_before_full_answer_finishes(self):
        second_segment_started = asyncio.Event()
        release_second_segment = asyncio.Event()

        async def fake_tts(segment, _voice_id):
            if segment == "Second sentence.":
                second_segment_started.set()
                await release_second_segment.wait()
            yield segment.encode()

        queue = asyncio.Queue()
        task = asyncio.create_task(
            queue_relay_speech(
                "Exact first sentence. Second sentence.",
                "voice",
                queue,
                fake_tts,
            )
        )
        await second_segment_started.wait()
        self.assertEqual(
            queue.get_nowait(),
            (b"Exact first sentence.", "Exact first sentence."),
        )
        self.assertFalse(task.done())
        release_second_segment.set()
        self.assertEqual(await task, 2)

        self.assertEqual(
            queue.get_nowait(),
            (b"Second sentence.", "Second sentence."),
        )

    async def test_relay_acknowledgement_is_complete_and_only_queued_once(self):
        async def fake_tts(_text, _voice_id):
            yield b"first"
            yield b"second"

        queue = asyncio.Queue()
        queued = await queue_relay_acknowledgement(
            "Let me check that for you.",
            "voice",
            queue,
            fake_tts,
            asyncio.Event(),
            delay=0,
        )

        self.assertTrue(queued)
        self.assertEqual(queue.qsize(), 2)
        self.assertEqual(
            queue.get_nowait(), (b"first", "Let me check that for you.")
        )
        self.assertEqual(
            queue.get_nowait(), (b"second", "Let me check that for you.")
        )

    async def test_instant_relay_answer_suppresses_acknowledgement(self):
        context_ready = asyncio.Event()
        synthesis_started = asyncio.Event()
        finish_synthesis = asyncio.Event()

        async def fake_tts(_text, _voice_id):
            synthesis_started.set()
            await finish_synthesis.wait()
            yield b"complete acknowledgement"

        queue = asyncio.Queue()
        task = asyncio.create_task(
            queue_relay_acknowledgement(
                "Let me check that for you.",
                "voice",
                queue,
                fake_tts,
                context_ready,
                delay=0,
            )
        )
        await synthesis_started.wait()
        context_ready.set()
        finish_synthesis.set()

        self.assertFalse(await task)
        self.assertTrue(queue.empty())


if __name__ == "__main__":
    unittest.main()
