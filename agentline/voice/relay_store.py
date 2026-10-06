"""Durable, turn-correlated state for live agent relay calls."""

import asyncio
import hashlib
import secrets
import time

from agentline.database import get_db_conn


TERMINAL_CALL_STATUSES = {"completed", "failed", "no-answer", "canceled", "cancelled"}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def find_turn_by_push_token(db, call_id: str, push_token: str) -> str | None:
    """Resolve a legacy token-only push without sacrificing turn correlation."""
    return await db.fetchval(
        """SELECT turn_id FROM relay_turns
           WHERE call_id=$1 AND push_token_hash=$2
             AND state IN ('waiting', 'answered')
           ORDER BY created_at DESC LIMIT 1""",
        call_id,
        _token_hash(push_token),
    )


async def create_relay_turn(
    db,
    *,
    call_id: str,
    turn_id: str,
    account_id: str,
    agent_id: str,
) -> str:
    """Create a waiting turn and return its one-turn push credential."""
    token = secrets.token_urlsafe(32)
    await db.execute(
        """INSERT INTO relay_turns
               (call_id, turn_id, account_id, agent_id, push_token_hash, state)
           VALUES ($1, $2, $3, $4, $5, 'waiting')""",
        call_id,
        turn_id,
        account_id,
        agent_id,
        _token_hash(token),
    )
    return token


async def deliver_turn_context(
    db,
    *,
    call_id: str,
    turn_id: str,
    context: str,
    push_token: str | None = None,
    account_id: str | None = None,
) -> str:
    """Store context atomically for one turn.

    Authentication is either the turn's push token or the owning account ID.
    Returns ``live``, ``duplicate``, ``stale``, ``ended``, ``unauthorized``,
    or ``not_found``.
    """
    row = await db.fetchrow(
        """SELECT rt.account_id, rt.agent_id, rt.push_token_hash, rt.state, rt.context,
                  c.status AS call_status, c.ended_at
           FROM relay_turns rt
           JOIN calls c ON c.id = rt.call_id
           WHERE rt.call_id=$1 AND rt.turn_id=$2""",
        call_id,
        turn_id,
    )
    if row is None:
        return "not_found"

    token_ok = bool(
        push_token
        and secrets.compare_digest(row["push_token_hash"], _token_hash(push_token))
    )
    account_ok = bool(account_id and row["account_id"] == account_id)
    if not token_ok and not account_ok:
        return "unauthorized"

    if row["ended_at"] is not None or row["call_status"] in TERMINAL_CALL_STATUSES:
        await db.execute(
            """UPDATE relay_turns SET state='expired'
               WHERE call_id=$1 AND turn_id=$2 AND state='waiting'""",
            call_id,
            turn_id,
        )
        return "ended"

    if row["state"] in {"answered", "consumed"}:
        return "duplicate" if row["context"] == context else "stale"
    if row["state"] != "waiting":
        return "stale"

    updated = await db.fetchval(
        """UPDATE relay_turns
           SET state='answered', context=$3, context_at=now()
           WHERE call_id=$1 AND turn_id=$2 AND state='waiting'
             AND EXISTS (
                 SELECT 1 FROM calls c
                 WHERE c.id=$1 AND c.ended_at IS NULL
                   AND c.status NOT IN ('completed', 'failed', 'no-answer', 'canceled', 'cancelled')
             )
           RETURNING turn_id""",
        call_id,
        turn_id,
        context,
    )
    if updated:
        await db.execute(
            """DELETE FROM event_mailbox
               WHERE account_id=$1 AND agent_id=$2
                 AND event_type='call.utterance'
                 AND payload->>'call_id'=$3 AND payload->>'turn_id'=$4""",
            row["account_id"],
            row["agent_id"],
            call_id,
            turn_id,
        )
        return "live"
    call_ended = await db.fetchval(
        """SELECT ended_at IS NOT NULL OR status IN
                  ('completed', 'failed', 'no-answer', 'canceled', 'cancelled')
           FROM calls WHERE id=$1""",
        call_id,
    )
    return "ended" if call_ended else "stale"


async def wait_for_turn_context(
    call_id: str,
    turn_id: str,
    *,
    timeout: float,
    poll_interval: float = 0.2,
) -> tuple[str | None, bool]:
    """Return ``(context, terminal)`` after context, turn end, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with get_db_conn() as db:
            row = await db.fetchrow(
                """SELECT state, context FROM relay_turns
                   WHERE call_id=$1 AND turn_id=$2""",
                call_id,
                turn_id,
            )
            if row is None or row["state"] not in {"waiting", "answered"}:
                return None, True
            if row["state"] == "answered":
                context = await db.fetchval(
                    """UPDATE relay_turns
                       SET state='consumed', consumed_at=now()
                       WHERE call_id=$1 AND turn_id=$2 AND state='answered'
                       RETURNING context""",
                    call_id,
                    turn_id,
                )
                if context is not None:
                    return context, False
        await asyncio.sleep(poll_interval)
    return None, False


async def cancel_relay_turn(call_id: str, turn_id: str) -> None:
    async with get_db_conn() as db:
        await db.execute(
            """UPDATE relay_turns SET state='cancelled', context=NULL
               WHERE call_id=$1 AND turn_id=$2
                 AND state IN ('waiting', 'answered')""",
            call_id,
            turn_id,
        )
        await db.execute(
            """DELETE FROM event_mailbox
               WHERE event_type='call.utterance'
                 AND payload->>'call_id'=$1 AND payload->>'turn_id'=$2""",
            call_id,
            turn_id,
        )


async def get_relay_transport(db, account_id: str, agent_id: str) -> str | None:
    """Return the preferred currently usable transport for an agent."""
    connected = await db.fetchval(
        """SELECT 1 FROM agent_connections
           WHERE account_id=$1 AND agent_id=$2 AND expires_at > now()""",
        account_id,
        agent_id,
    )
    if connected:
        return "websocket"
    webhook = await db.fetchval(
        "SELECT 1 FROM webhooks WHERE account_id=$1 AND agent_id=$2",
        account_id,
        agent_id,
    )
    return "webhook" if webhook else None
