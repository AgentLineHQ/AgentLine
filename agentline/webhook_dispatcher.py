"""
AgentLine — Webhook Dispatcher
Fires webhook payloads to customer-registered webhook URLs.

Looks up webhooks from the `webhooks` table by account_id + optional agent_id,
signs the payload with the webhook secret, and POSTs it.
"""

import hashlib
import hmac
import json
import logging

import httpx

from agentline.database import get_db_conn

logger = logging.getLogger(__name__)


async def dispatch_webhook(
    account_id: str,
    agent_id: str | None,
    payload: dict,
) -> None:
    """
    Dispatch an event payload to all matching customer webhooks.

    Looks up webhooks by:
      1. Agent-specific webhooks (agent_id match)
      2. Account-level webhooks (agent_id IS NULL)

    Signs the payload with HMAC-SHA256 using the webhook's secret,
    and sends the header `X-AgentLine-Signature` for verification.
    """
    try:
        async with get_db_conn() as db:
            # Get all matching webhooks: agent-specific + account-level
            rows = await db.fetch(
                """SELECT id, url, secret FROM webhooks
                   WHERE account_id = $1
                     AND (agent_id = $2 OR agent_id IS NULL)""",
                account_id, agent_id,
            )

        if not rows:
            return

        body = json.dumps(payload, default=str)

        async with httpx.AsyncClient(timeout=10.0) as client:
            for row in rows:
                try:
                    # Sign the payload
                    signature = hmac.new(
                        row["secret"].encode(),
                        body.encode(),
                        hashlib.sha256,
                    ).hexdigest()

                    await client.post(
                        row["url"],
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-AgentLine-Signature": signature,
                            "X-AgentLine-Event": payload.get("event", "unknown"),
                        },
                    )
                    logger.info(
                        "Webhook dispatched to %s (webhook=%s, event=%s)",
                        row["url"], row["id"][:12], payload.get("event"),
                    )
                except Exception as e:
                    logger.warning(
                        "Webhook delivery failed for %s: %s", row["url"], e,
                    )
    except Exception as e:
        logger.warning("Webhook dispatch error: %s", e)
