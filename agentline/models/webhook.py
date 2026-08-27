"""
AgentLine — Webhook Pydantic schemas

Webhooks are strictly per-agent: each agent has at most ONE webhook URL that
receives ALL of that agent's events (calls, SMS, future agent-driven data) as
signed JSON POSTs. There is no account-wide webhook.
"""

from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class WebhookConfig(BaseModel):
    """Body for POST /v1/webhooks (create or replace an agent's webhook)."""

    url: HttpUrl = Field(
        description=(
            "HTTPS URL that will receive this agent's events as signed JSON "
            "POSTs. Setting this replaces any existing webhook for the agent."
        )
    )
    agent_id: str = Field(
        description=(
            "ID of the agent whose events this webhook receives. Each agent "
            "may have at most one webhook."
        )
    )
    secret: str | None = Field(
        default=None,
        description=(
            "Optional HMAC signing secret. Auto-generated if omitted. Used to "
            "verify the signature header on delivered payloads."
        ),
    )
    signature_header: str | None = Field(
        default=None,
        description=(
            "Header name for the HMAC-SHA256 signature of the raw body. "
            "Defaults to 'X-Webhook-Signature' (natively verified by Hermes, "
            "OpenClaw, and other agent platforms). Set to 'X-Hub-Signature-256' "
            "for GitHub-style verification, or any custom header name your "
            "platform expects. Omit to keep the existing value when updating."
        ),
    )


class WebhookOut(BaseModel):
    """One webhook row. `secret` is masked on list reads; the full secret is only
    returned on the POST that creates/replaces the webhook."""

    agent_id: str = Field(description="Agent this webhook is scoped to")
    url: str = Field(description="Configured webhook URL")
    secret: str = Field(description="Signing secret (masked on reads). Full value shown only on create/replace.")
    signature_header: str = Field(description="Header name used for HMAC-SHA256 signature delivery.")
    created_at: datetime | None = Field(default=None, description="When the webhook was last (re)configured")


class WebhookCreated(WebhookOut):
    """Returned on POST — exposes the full secret this one time."""

    secret: str = Field(description="Full signing secret. Save it now — it is masked on subsequent reads.")
