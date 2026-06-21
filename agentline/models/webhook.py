"""
AgentLine — Webhook Pydantic schemas
"""

from pydantic import BaseModel, HttpUrl
from datetime import datetime


class WebhookCreate(BaseModel):
    url: str
    agent_id: str | None = None  # NULL = account-level webhook


class WebhookUpdate(BaseModel):
    url: str | None = None
    agent_id: str | None = None


class WebhookOut(BaseModel):
    id: str
    account_id: str
    agent_id: str | None = None
    url: str
    secret: str
    created_at: datetime
