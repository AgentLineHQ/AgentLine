"""
AgentLine — Webhooks Router
Configure per-agent webhooks. Each agent has at most ONE webhook URL that
receives ALL of that agent's events as signed JSON POSTs. There is no
account-wide webhook. POST /v1/webhooks/test fires a signed test payload through
the same event bus (agentline.event_bus.publish_event) that real events use.
"""

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query

from agentline.auth_middleware import get_current_account
from agentline.database import get_db
from agentline.event_bus import publish_event
from agentline.models.webhook import WebhookConfig, WebhookCreated
from agentline.webhook_dispatcher import clear_webhook_health

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["Webhooks"])

_SECRET_PREFIX = "whsec_"


def _mask_secret(secret: str | None) -> str:
    if not secret:
        return ""
    if len(secret) <= 12:
        return secret[:4] + "…" if len(secret) > 4 else "…"
    return f"{secret[:10]}…{secret[-4:]}"


async def _assert_agent_owned(db, account_id: str, agent_id: str) -> None:
    row = await db.fetchrow(
        "SELECT id FROM agents WHERE id = $1 AND account_id = $2",
        agent_id, account_id,
    )
    if not row:
        raise HTTPException(404, "Agent not found.")


@router.get("", operation_id="get_webhook")
async def get_webhooks(
    agent_id: str | None = Query(None, description="Inspect one agent's webhook (omit to list all)"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    List the account's per-agent webhook configuration(s).

    Secrets are **masked**. Pass `agent_id` to inspect a single agent's webhook.
    The full secret is only ever shown once, on the POST that creates/replaces it.
    """
    if agent_id:
        await _assert_agent_owned(db, account["id"], agent_id)
        rows = await db.fetch(
            """SELECT agent_id, url, secret, signature_header, created_at FROM webhooks
               WHERE account_id = $1 AND agent_id = $2""",
            account["id"], agent_id,
        )
    else:
        rows = await db.fetch(
            """SELECT agent_id, url, secret, signature_header, created_at FROM webhooks
               WHERE account_id = $1 ORDER BY created_at DESC""",
            account["id"],
        )

    return {
        "webhooks": [
            {
                "agent_id": r["agent_id"],
                "url": r["url"],
                "secret": _mask_secret(r["secret"]),
                "signature_header": r["signature_header"] or "X-Webhook-Signature",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.post("", response_model=WebhookCreated, operation_id="set_webhook")
async def set_webhook(
    body: WebhookConfig,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Create or replace an agent's webhook.

    The configured URL receives ALL of that agent's event types — call lifecycle
    (`call.received`, `call.completed`, `call.failed`), SMS (`sms.received`),
    and future events — as signed JSON POSTs. Each agent may have at most one
    webhook; POSTing again replaces it.

    - `agent_id`: the agent whose events this webhook receives (required).
    - `secret`:   HMAC signing secret. Omit to auto-generate.

    The response returns the full `secret` **once** — store it to verify the
    signature header on deliveries.
    """
    url = str(body.url)
    if url.startswith("http://"):
        logger.warning(
            "Account %s configured a non-HTTPS webhook for agent %s (%s)",
            account["id"][:12], body.agent_id[:12], url,
        )

    await _assert_agent_owned(db, account["id"], body.agent_id)

    webhook_id = f"wh_{secrets.token_urlsafe(12)}"

    existing = await db.fetchrow(
        """SELECT id, secret, signature_header FROM webhooks
           WHERE account_id = $1 AND agent_id = $2""",
        account["id"], body.agent_id,
    )

    # Secret resolution:
    #   1. Caller provided one explicitly → use it
    #   2. Updating an existing webhook, caller omitted secret → keep existing
    #   3. Brand new webhook, caller omitted secret → auto-generate
    if body.secret:
        secret = body.secret
    elif existing:
        secret = existing["secret"]
    else:
        secret = f"{_SECRET_PREFIX}{secrets.token_urlsafe(32)}"

    # Signature header resolution:
    #   1. Caller provided one explicitly → use it
    #   2. Updating an existing webhook, caller omitted → keep existing
    #   3. Brand new webhook, caller omitted → default
    if body.signature_header:
        sig_header = body.signature_header
    elif existing and existing["signature_header"]:
        sig_header = existing["signature_header"]
    else:
        sig_header = "X-Webhook-Signature"

    # New/updated URL — drop any "known dead" state so relay can try again.
    clear_webhook_health(body.agent_id)

    if existing:
        row = await db.fetchrow(
            """UPDATE webhooks SET url = $1, secret = $2, signature_header = $3, created_at = now()
               WHERE id = $4
               RETURNING agent_id, url, secret, signature_header, created_at""",
            url, secret, sig_header, existing["id"],
        )
        logger.info(
            "Webhook (re)configured for account %s agent=%s (sig header: %s)",
            account["id"][:12], body.agent_id[:12], sig_header,
        )
    else:
        row = await db.fetchrow(
            """INSERT INTO webhooks (id, account_id, agent_id, url, secret, signature_header)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING agent_id, url, secret, signature_header, created_at""",
            webhook_id, account["id"], body.agent_id, url, secret, sig_header,
        )
        logger.info(
            "Webhook created for account %s agent=%s (sig header: %s)",
            account["id"][:12], body.agent_id[:12], sig_header,
        )

    return WebhookCreated(
        agent_id=row["agent_id"],
        url=row["url"],
        secret=row["secret"],
        signature_header=row["signature_header"] or "X-Webhook-Signature",
        created_at=row["created_at"],
    )


@router.delete("", operation_id="delete_webhook")
async def delete_webhook(
    agent_id: str = Query(..., description="Agent whose webhook to delete"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Remove an agent's webhook. No events for that agent will be delivered via
    HTTP afterwards; they remain available via GET /v1/events (the mailbox).
    """
    await db.execute(
        "DELETE FROM webhooks WHERE account_id = $1 AND agent_id = $2",
        account["id"], agent_id,
    )
    clear_webhook_health(agent_id)
    logger.info(
        "Webhook deleted for account %s agent=%s",
        account["id"][:12], agent_id[:12],
    )
    return {"deleted": True, "agent_id": agent_id}


@router.post("/test", operation_id="test_webhook")
async def test_webhook(
    agent_id: str = Query(..., description="Agent whose webhook to test"),
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Fire a signed `webhook.test` event to the agent's webhook.

    Uses the exact same event bus (publish_event) that real telephony events use,
    so a successful delivery confirms the entire pipeline is wired correctly.
    Returns 404 if no webhook is configured for the agent.
    """
    row = await db.fetchrow(
        "SELECT url FROM webhooks WHERE account_id = $1 AND agent_id = $2",
        account["id"], agent_id,
    )
    if not row:
        raise HTTPException(404, "No webhook configured for this agent. POST /v1/webhooks first.")

    await publish_event(
        account_id=account["id"],
        agent_id=agent_id,
        event_type="webhook.test",
        payload={
            "message": "This is a test event from AgentLine.",
            "url": row["url"],
        },
    )
    return {"sent": True, "url": row["url"], "event_type": "webhook.test"}
