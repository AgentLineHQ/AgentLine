"""
AgentLine — Events Router
Server-side event mailbox for agents that can't expose webhooks.

When calls complete, transcripts are pushed here automatically.
Agents poll GET /v1/events to receive them.
"""

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from agentline.auth_middleware import get_current_account, resolve_account
from agentline.config import settings
from agentline.database import get_db, get_db_conn
from agentline.voice.relay_context import extract_context
from agentline.voice.relay_store import deliver_turn_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/events", tags=["Events"])


@router.get("", operation_id="poll_events")
async def list_events(
    agent_id: str | None = Query(None, description="Filter events by AI agent ID"),
    event_type: str | None = Query(None, description="Filter by event type (e.g. 'call.completed', 'call.failed')"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of events to return (1-200)"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Poll for telephony events from your AI agents.

    Returns pending events such as call completions, transcripts, and
    failures. Events are consumed on retrieval (one-time read) — once
    polled, they are automatically deleted from the mailbox.

    Your AI agent should call this endpoint periodically to receive
    notifications about completed calls and their transcripts.

    Filters:
      - agent_id: only events for a specific AI agent
      - event_type: e.g. "call.completed", "call.failed"
    """
    conditions = ["account_id = $1"]
    params: list = [account["id"]]
    idx = 2

    if agent_id:
        conditions.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1

    if event_type:
        conditions.append(f"event_type = ${idx}")
        params.append(event_type)
        idx += 1

    where = " AND ".join(conditions)
    params.append(limit)

    # Claim and delete atomically. SKIP LOCKED prevents concurrent pollers from
    # receiving the same rows. Live integrations should use /v1/events/ws,
    # which retains an event until the client explicitly acknowledges it.
    rows = await db.fetch(
        f"""WITH claimed AS (
               SELECT id FROM event_mailbox
               WHERE {where}
               ORDER BY created_at ASC
               FOR UPDATE SKIP LOCKED
               LIMIT ${idx}
           )
           DELETE FROM event_mailbox e
           USING claimed c
           WHERE e.id = c.id
           RETURNING e.id, e.event_id, e.agent_id, e.event_type,
                     e.payload, e.created_at""",
        *params,
    )

    events = []
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                pass

        events.append({
            "event_id": row["event_id"],
            "agent_id": row["agent_id"],
            "event_type": row["event_type"],
            "payload": payload,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        })
    if events:
        logger.info("Delivered %d events to account %s", len(events), account["id"][:12])

    return {
        "events": events,
        "count": len(events),
    }


def _event_envelope(row) -> dict:
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "type": "event",
        "event_id": row["event_id"],
        "agent_id": row["agent_id"],
        "event_type": row["event_type"],
        "payload": payload,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.websocket("/ws")
async def agent_event_websocket(websocket: WebSocket):
    """Reliable outbound connection for Hermes, OpenClaw, Claude, Codex, or any agent.

    The client connects to AgentLine, so no public listener is required. One
    mailbox event is delivered at a time and remains durable until an ``ack``
    frame is received. Live context can be returned on the same socket.
    """
    authorization = websocket.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        await websocket.close(code=4401, reason="Bearer authentication required")
        return

    agent_id = websocket.query_params.get("agent_id")
    runtime = (websocket.query_params.get("runtime") or "unknown")[:40]
    if not agent_id:
        await websocket.close(code=4400, reason="agent_id is required")
        return

    token = authorization.split(" ", 1)[1].strip()
    async with get_db_conn() as db:
        account = await resolve_account(token, db)
        if account is None:
            await websocket.close(code=4401, reason="Invalid credential")
            return
        owns_agent = await db.fetchval(
            "SELECT 1 FROM agents WHERE id=$1 AND account_id=$2",
            agent_id,
            account["id"],
        )
        if not owns_agent:
            await websocket.close(code=4404, reason="Agent not found")
            return

    await websocket.accept()
    connection_id = f"conn_{secrets.token_urlsafe(12)}"

    async with get_db_conn() as db:
        await db.execute(
            """INSERT INTO agent_connections
                   (agent_id, account_id, connection_id, runtime, expires_at, updated_at)
               VALUES ($1, $2, $3, $4, now() + interval '45 seconds', now())
               ON CONFLICT (agent_id) DO UPDATE SET
                   account_id=EXCLUDED.account_id,
                   connection_id=EXCLUDED.connection_id,
                   runtime=EXCLUDED.runtime,
                   expires_at=EXCLUDED.expires_at,
                   updated_at=now()""",
            agent_id,
            account["id"],
            connection_id,
            runtime,
        )

    await websocket.send_json({
        "type": "hello",
        "protocol": "agentline-relay/1",
        "connection_id": connection_id,
        "runtime": runtime,
        "agent_id": agent_id,
        "session_rule": "Use session_key from each event. Keep all turns with the same call_id in that runtime session.",
        "instructions": [
            "Send {\"type\":\"ping\"} at least every 30 seconds to keep this transport active.",
            "Acknowledge each event with {\"type\":\"ack\",\"event_id\":\"...\"}.",
            "For call.utterance, do the requested work immediately.",
            "Reply with a context frame echoing call_id, turn_id, and push_token.",
            "Never reuse context for a different turn_id.",
        ],
        "discovery": f"{settings.base_url_clean}/.well-known/agentline.json",
    })

    incoming: asyncio.Queue = asyncio.Queue(maxsize=100)

    async def _receive():
        try:
            while True:
                await incoming.put(await websocket.receive_json())
        except (WebSocketDisconnect, RuntimeError, ValueError, json.JSONDecodeError):
            await incoming.put(None)
        except Exception:
            logger.exception("Agent event WebSocket receiver failed")
            await incoming.put(None)

    receiver = asyncio.create_task(_receive())
    outstanding: dict[str, dict] = {}
    last_heartbeat = time.monotonic()
    try:
        while True:
            async with get_db_conn() as db:
                current_connection = await db.fetchval(
                    """SELECT connection_id FROM agent_connections
                       WHERE agent_id=$1 AND expires_at > now()""",
                    agent_id,
                )
            if current_connection != connection_id:
                await websocket.close(code=4001, reason="Replaced by a newer connection")
                break

            if outstanding:
                async with get_db_conn() as db:
                    pending_ids = await db.fetch(
                        """SELECT event_id FROM event_mailbox
                           WHERE account_id=$1 AND agent_id=$2
                             AND event_id = ANY($3::text[])""",
                        account["id"],
                        agent_id,
                        list(outstanding),
                    )
                pending_set = {row["event_id"] for row in pending_ids}
                outstanding = {
                    event_id: event
                    for event_id, event in outstanding.items()
                    if event_id in pending_set
                }

            # Multiple calls can be live for one agent. Send every distinct
            # call.utterance without waiting for another call's ACK, while
            # limiting ordinary events to one in flight.
            ordinary_in_flight = any(
                event["event_type"] != "call.utterance"
                for event in outstanding.values()
            )
            async with get_db_conn() as db:
                row = await db.fetchrow(
                    """SELECT event_id, agent_id, event_type, payload, created_at
                       FROM event_mailbox
                       WHERE account_id=$1 AND agent_id=$2
                         AND NOT (event_id = ANY($3::text[]))
                         AND (event_type='call.utterance' OR NOT $4::boolean)
                       ORDER BY CASE WHEN event_type='call.utterance' THEN 0 ELSE 1 END,
                                created_at ASC
                       LIMIT 1""",
                    account["id"],
                    agent_id,
                    list(outstanding),
                    ordinary_in_flight,
                )
            if row:
                event = _event_envelope(row)
                outstanding[event["event_id"]] = event
                await websocket.send_json(event)

            try:
                message = await asyncio.wait_for(incoming.get(), timeout=1.0)
            except asyncio.TimeoutError:
                message = ...
            if message is None:
                break
            if message is not ...:
                message_type = message.get("type") if isinstance(message, dict) else None
                async with get_db_conn() as db:
                    await db.execute(
                        """UPDATE agent_connections
                           SET expires_at=now() + interval '45 seconds', updated_at=now()
                           WHERE agent_id=$1 AND connection_id=$2""",
                        agent_id,
                        connection_id,
                    )
                if message_type == "ack":
                    event_id = message.get("event_id")
                    if event_id:
                        async with get_db_conn() as db:
                            deleted = await db.fetchval(
                                """DELETE FROM event_mailbox
                                   WHERE account_id=$1 AND agent_id=$2 AND event_id=$3
                                   RETURNING event_id""",
                                account["id"],
                                agent_id,
                                event_id,
                            )
                        outstanding.pop(event_id, None)
                        await websocket.send_json({
                            "type": "acked",
                            "event_id": event_id,
                            "duplicate": deleted is None,
                        })
                    else:
                        await websocket.send_json({"type": "error", "code": "unexpected_ack"})
                elif message_type == "context":
                    call_id = message.get("call_id")
                    turn_id = message.get("turn_id")
                    context = extract_context(message)
                    if not call_id or not turn_id or not context:
                        await websocket.send_json({
                            "type": "error",
                            "code": "invalid_context",
                            "message": "call_id, turn_id, and context are required",
                        })
                        continue
                    async with get_db_conn() as db:
                        status = await deliver_turn_context(
                            db,
                            call_id=call_id,
                            turn_id=turn_id,
                            context=context,
                            push_token=message.get("push_token"),
                            account_id=account["id"],
                        )
                    await websocket.send_json({
                        "type": "context_result",
                        "event_id": message.get("event_id"),
                        "call_id": call_id,
                        "turn_id": turn_id,
                        "status": status,
                    })
                elif message_type == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    await websocket.send_json({"type": "error", "code": "unknown_frame"})

            if time.monotonic() - last_heartbeat >= 15:
                await websocket.send_json({
                    "type": "heartbeat",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "reply_with": {"type": "ping"},
                })
                last_heartbeat = time.monotonic()
    finally:
        receiver.cancel()
        async with get_db_conn() as db:
            await db.execute(
                "DELETE FROM agent_connections WHERE agent_id=$1 AND connection_id=$2",
                agent_id,
                connection_id,
            )


@router.get("/peek", operation_id="peek_events")
async def peek_events(
    agent_id: str | None = Query(None, description="Filter events by AI agent ID"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of events to preview (1-200)"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Peek at pending telephony events without consuming them.

    Returns a preview of queued events (call completions, transcripts)
    without removing them from the mailbox. Useful for checking if
    there are events to process before committing to retrieve them.
    """
    conditions = ["account_id = $1"]
    params: list = [account["id"]]
    idx = 2

    if agent_id:
        conditions.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1

    where = " AND ".join(conditions)
    params.append(limit)

    count = await db.fetchval(
        f"SELECT COUNT(*) FROM event_mailbox WHERE {where}",
        *params[:-1],  # exclude limit for count
    )

    rows = await db.fetch(
        f"""SELECT event_id, agent_id, event_type, created_at
           FROM event_mailbox
           WHERE {where}
           ORDER BY created_at ASC
           LIMIT ${idx}""",
        *params,
    )

    return {
        "pending_count": count,
        "events": [
            {
                "event_id": row["event_id"],
                "agent_id": row["agent_id"],
                "event_type": row["event_type"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ],
    }
