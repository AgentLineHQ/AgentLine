"""
AgentLine — Event Bus
The single, canonical entry point for publishing events in AgentLine.

Every event — call lifecycle, SMS, billing, and future agent-driven data — flows
through `publish_event()`. It normally fans out to two delivery channels:

  1. event_mailbox  — durable, consume-once queue polled via GET /v1/events
  2. webhook        — signed HTTP POST to the account's configured webhook URL

Live relay turns select exactly one transport to prevent duplicate work; other
events fan out to both channels. Future features must call `publish_event()`
rather than inserting or dispatching directly.

Usage:
    from agentline.event_bus import publish_event

    await publish_event(
        account_id=account["id"],
        agent_id=agent_id,
        event_type="my_feature.thing_happened",
        payload={"foo": "bar"},
    )
"""

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone

from agentline.database import get_db_conn
from agentline.webhook_dispatcher import dispatch_webhook, dispatch_webhook_await_response

logger = logging.getLogger(__name__)


async def publish_event(
    account_id: str,
    agent_id: str | None,
    event_type: str,
    payload: dict,
    await_webhook_response: bool = False,
    webhook_timeout: float | None = 15.0,
    deliver_webhook: bool = True,
    persist_mailbox: bool = True,
    require_mailbox: bool = False,
) -> dict | None:
    """
    Publish an event to the mailbox and/or webhook.

    This is THE function to call when anything noteworthy happens in AgentLine.
    It is fire-and-forget and never raises: each channel is isolated, so a
    webhook failure never blocks the mailbox insert (or vice versa), protecting
    callers that must return a response (e.g. provider callback handlers).

    **Critical for telephony:** when ``await_webhook_response`` is False, the
    webhook HTTP POST is scheduled as a background task and this function
    returns immediately after the mailbox insert. A dead / slow / missing
    webhook must NEVER delay SignalWire LaML responses (inbound answer XML,
    hangup handlers, SMS callbacks) — that previously caused duration=0
    ``failed`` / ``no-answer`` because Stream never started.

    Args:
        account_id: Owning account.
        agent_id:   Related agent if any (None for account-level events). Passed
                    through into the payload so receivers know which agent fired.
        event_type: Dotted event name, e.g. "call.completed", "sms.received".
        payload:    Event-specific data (bare — the "event" key is added
                    automatically for the webhook envelope).
        await_webhook_response: When True, the webhook dispatch WAITS for the
                    agent's HTTP response body and returns it as a dict.  Used
                    exclusively by relay-mode ``call.utterance`` events so the
                    agent can inject context back into the live call via the
                    webhook response.  When False (default), the webhook is
                    truly fire-and-forget (background task).
        webhook_timeout: Timeout (seconds) for the webhook dispatch when
                    ``await_webhook_response`` is True.  ``None`` means no
                    HTTP timeout (relay mode).  Ignored otherwise.
        deliver_webhook: False when an active agent WebSocket owns delivery.
        persist_mailbox: False for synchronous webhook relay turns so they
                    cannot replay later through a different transport.
        require_mailbox: Raise if a required live WebSocket event cannot be
                    persisted instead of silently waiting for impossible work.

    Returns:
        When ``await_webhook_response`` is True: the parsed JSON response dict
        from the agent's webhook, or None on failure/timeout/no-webhook.
        When False: None (fire-and-forget, as before).
    """
    event_id = f"evt_{secrets.token_urlsafe(12)}"
    body = json.dumps(payload, default=str)

    # 1. Persist to the event mailbox (consume-once polling queue for /v1/events)
    #    This is the only await on the hot path for non-relay events — DB only.
    if persist_mailbox:
        try:
            async with get_db_conn() as db:
                await db.execute(
                    """INSERT INTO event_mailbox
                       (event_id, account_id, agent_id, event_type, payload)
                       VALUES ($1, $2, $3, $4, $5)""",
                    event_id, account_id, agent_id, event_type, body,
                )
        except Exception as e:
            logger.error("publish_event[%s]: mailbox insert failed: %s", event_type, e)
            if require_mailbox:
                raise

    # 2. Fire to the configured webhook (signed, best-effort)
    # Webhook envelope exposes the event type under BOTH `event_type` (the
    # canonical name used by the Events Mailbox and most webhook consumers,
    # e.g. Hermes/Stripe-style filters) and `event` (legacy alias). Envelope
    # keys are placed AFTER **payload so a payload key can never shadow them.
    # event_id/agent_id/account_id are included so receivers can correlate and
    # dedupe, matching the Events Mailbox row shape.
    webhook_envelope = {
        **payload,
        "event_id": event_id,
        "agent_id": agent_id,
        "account_id": account_id,
        "event_type": event_type,
        "event": event_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if not deliver_webhook:
        return None

    if await_webhook_response:
        # Relay path only — pipeline explicitly wants the agent response body.
        try:
            return await dispatch_webhook_await_response(
                account_id, agent_id, webhook_envelope,
                timeout=webhook_timeout,
            )
        except Exception as e:
            logger.warning("publish_event[%s]: webhook await-response failed: %s", event_type, e)
            return None

    # Background delivery: never block inbound answer / hangup / SMS handlers.
    # dispatch_webhook already no-ops when no webhook is configured and
    # swallows delivery errors (logs a warning).
    try:
        task = asyncio.create_task(
            dispatch_webhook(account_id, agent_id, webhook_envelope),
            name=f"webhook:{event_type}:{event_id[:16]}",
        )

        def _done(t: asyncio.Task) -> None:
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            except asyncio.InvalidStateError:
                return
            if exc is not None:
                logger.warning(
                    "publish_event[%s]: background webhook task failed: %s",
                    event_type, exc,
                )

        task.add_done_callback(_done)
    except Exception as e:
        logger.warning("publish_event[%s]: failed to schedule webhook: %s", event_type, e)
    return None
