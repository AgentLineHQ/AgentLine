"""
AgentLine — Webhook Dispatcher
Low-level delivery layer that POSTs a signed payload to an agent's webhook.

This module is the "delivery" half of the event pipeline. The "publishing" half
lives in agentline.event_bus.publish_event(), which is the public entry point all
application code should call. dispatch_webhook() is only invoked by
publish_event() and the /v1/webhooks/test endpoint.

Webhooks are strictly per-agent (one webhook URL per agent). There is NO
account-wide webhook — an event is delivered only when the agent it belongs to
has a webhook configured.

Also tracks in-memory health so a dead agent URL does not keep enabling
relay mode / ``call.utterance`` (which stalls the live caller for context).
"""

import hashlib
import hmac
import json
import logging
import time

import httpx

from agentline.database import get_db_conn

logger = logging.getLogger(__name__)

# agent_id -> (healthy: bool, marked_at: monotonic seconds)
# Used so a dead webhook does not keep turning on relay / call.utterance.
_webhook_health: dict[str, tuple[bool, float]] = {}

# After this many seconds, allow another delivery attempt (agent may have recovered).
_WEBHOOK_DEAD_TTL_SECONDS = 300.0

# Connect-only timeout: fail fast when the agent host is unreachable.
_CONNECT_TIMEOUT = 2.0


def mark_webhook_healthy(agent_id: str | None) -> None:
    if agent_id:
        _webhook_health[agent_id] = (True, time.monotonic())


def mark_webhook_dead(agent_id: str | None) -> None:
    if agent_id:
        _webhook_health[agent_id] = (False, time.monotonic())
        logger.warning(
            "Webhook marked DEAD for agent %s — relay/call.utterance disabled "
            "until recovery or %.0fs TTL",
            agent_id[:12], _WEBHOOK_DEAD_TTL_SECONDS,
        )


def is_webhook_known_dead(agent_id: str | None) -> bool:
    """True when the last delivery hard-failed and the dead TTL has not expired."""
    if not agent_id:
        return False
    entry = _webhook_health.get(agent_id)
    if not entry:
        return False
    healthy, marked_at = entry
    if healthy:
        return False
    if (time.monotonic() - marked_at) >= _WEBHOOK_DEAD_TTL_SECONDS:
        # TTL expired — allow a retry (cleared on next success/failure).
        return False
    return True


def clear_webhook_health(agent_id: str | None) -> None:
    """Drop health state (e.g. when the webhook URL is replaced)."""
    if agent_id:
        _webhook_health.pop(agent_id, None)


def _http_timeout(total: float | None) -> httpx.Timeout:
    """Build a timeout with a short connect phase so dead hosts fail fast."""
    if total is None:
        return httpx.Timeout(None, connect=_CONNECT_TIMEOUT)
    return httpx.Timeout(total, connect=min(_CONNECT_TIMEOUT, total))


async def dispatch_webhook(
    account_id: str,
    agent_id: str | None,
    payload: dict,
) -> None:
    """
    Deliver `payload` to the webhook configured for (account_id, agent_id).

    Looks up the single per-agent webhook row, signs the JSON body with
    HMAC-SHA256 using the webhook's secret, and POSTs it with headers:

      - <signature_header>: HMAC-SHA256 hex digest of the raw body.
        Header name is configurable per webhook (default: X-Webhook-Signature).
      - X-AgentLine-Event:  <payload["event_type"]> (canonical; falls back to payload["event"])

    No-op when no webhook is configured for the agent (or when agent_id is
    None). Failures are logged, never raised.

    Callers must not await this on telephony hot paths — publish_event()
    schedules it as a background task so a dead webhook cannot delay
    SignalWire LaML responses.

    Note: dispatch_webhook does NOT persist to the event mailbox — that is the
    job of agentline.event_bus.publish_event(). Call that instead.
    """
    if not agent_id:
        return

    if is_webhook_known_dead(agent_id):
        logger.info(
            "Skipping webhook dispatch for agent %s — known dead",
            agent_id[:12],
        )
        return

    try:
        async with get_db_conn() as db:
            row = await db.fetchrow(
                """SELECT id, url, secret, signature_header FROM webhooks
                   WHERE account_id = $1 AND agent_id = $2""",
                account_id, agent_id,
            )

        if not row:
            return

        body = json.dumps(payload, default=str)
        signature = hmac.new(
            row["secret"].encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()

        sig_header = row["signature_header"] or "X-Webhook-Signature"

        # Short timeout: background delivery only. Do not hold connections
        # open for long on dead agent endpoints.
        async with httpx.AsyncClient(timeout=_http_timeout(5.0)) as client:
            resp = await client.post(
                row["url"],
                content=body,
                headers={
                    "Content-Type": "application/json",
                    sig_header: signature,
                    "X-AgentLine-Event": payload.get("event_type", payload.get("event", "unknown")),
                },
            )

        logger.info(
            "Webhook delivered to %s (webhook=%s, agent=%s, event_type=%s, status=%s)",
            row["url"], row["id"][:12], agent_id[:12], payload.get("event_type", payload.get("event")), resp.status_code,
        )

        if resp.status_code >= 500:
            mark_webhook_dead(agent_id)
        else:
            # 2xx/3xx/4xx means the host is reachable — relay may use it.
            mark_webhook_healthy(agent_id)
    except Exception as e:
        mark_webhook_dead(agent_id)
        logger.warning("Webhook delivery failed for agent %s: %s", agent_id[:12], e)


async def dispatch_webhook_await_response(
    account_id: str,
    agent_id: str | None,
    payload: dict,
    timeout: float | None = 15.0,
) -> dict | None:
    """
    Deliver `payload` to the agent's webhook and WAIT for the HTTP response body.

    This is the bidirectional variant of dispatch_webhook(), used exclusively for
    ``call.utterance`` events in relay mode.  The agent's webhook handler can
    return context in the response body — AgentLine reads it and injects it into
    the live call's system prompt so the hosted LLM can answer immediately.

    Expected agent response format (JSON):
        {"context": "short caller-ready response spoken verbatim"}

    Returns the parsed JSON response dict, or None when:
      - no webhook is configured for the agent
      - agent_id is None
      - the request fails or times out
      - the response body is not valid JSON
      - the webhook is known-dead (skip entirely — no call.utterance hammering)

    Failures are logged, never raised — the caller (the voice pipeline) treats
    None as "no context available" and falls back to hosted mode when dead.
    """
    if not agent_id:
        return None

    if is_webhook_known_dead(agent_id):
        logger.info(
            "Skipping await-response webhook for agent %s — known dead",
            agent_id[:12],
        )
        return None

    try:
        async with get_db_conn() as db:
            row = await db.fetchrow(
                """SELECT id, url, secret, signature_header FROM webhooks
                   WHERE account_id = $1 AND agent_id = $2""",
                account_id, agent_id,
            )

        if not row:
            return None

        body = json.dumps(payload, default=str)
        signature = hmac.new(
            row["secret"].encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()

        sig_header = row["signature_header"] or "X-Webhook-Signature"

        async with httpx.AsyncClient(timeout=_http_timeout(timeout)) as client:
            resp = await client.post(
                row["url"],
                content=body,
                headers={
                    "Content-Type": "application/json",
                    sig_header: signature,
                    "X-AgentLine-Event": payload.get("event_type", payload.get("event", "unknown")),
                },
            )

        logger.info(
            "Webhook (await-response) delivered to %s (webhook=%s, agent=%s, "
            "event_type=%s, status=%s)",
            row["url"], row["id"][:12], agent_id[:12],
            payload.get("event_type", payload.get("event")), resp.status_code,
        )

        # Log the raw response body so relay-mode context delivery can be
        # diagnosed: shows whether the backend returned context, an empty ack,
        # or a non-JSON body (e.g. when it routes the reply to WhatsApp instead).
        try:
            preview = resp.text[:300]
        except Exception:
            preview = "<unreadable>"
        logger.info(
            "Webhook await-response body (agent=%s): %r",
            agent_id[:12], preview,
        )

        if resp.status_code >= 500:
            mark_webhook_dead(agent_id)
            return None

        if resp.status_code >= 400:
            # Host is up but rejected the request — do not mark dead.
            mark_webhook_healthy(agent_id)
            logger.warning(
                "Webhook returned HTTP %d for agent %s — no context will be applied",
                resp.status_code, agent_id[:12],
            )
            return None

        mark_webhook_healthy(agent_id)

        try:
            return resp.json()
        except Exception:
            logger.debug(
                "Webhook response body was not JSON for agent %s — ignoring",
                agent_id[:12],
            )
            return None

    except httpx.TimeoutException:
        # Soft timeout: host may still be alive; agent can push via POST.
        # Do NOT mark dead — slow agents should not kill relay for the account.
        logger.info(
            "Webhook await-response timed out (%s) for agent %s — "
            "voice agent will keep waiting for push context",
            f"{timeout:.0f}s" if timeout is not None else "none",
            agent_id[:12],
        )
        return None
    except Exception as e:
        # Connect errors, remote disconnect, DNS, etc. → dead webhook.
        mark_webhook_dead(agent_id)
        logger.warning(
            "Webhook await-response failed for agent %s: %s", agent_id[:12], e
        )
        return None
