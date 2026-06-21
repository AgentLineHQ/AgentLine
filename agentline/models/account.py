"""
AgentLine — Account & API Key Pydantic schemas
Used for request/response validation; actual storage is raw SQL via asyncpg.
"""

from pydantic import BaseModel, EmailStr
from datetime import datetime


class AccountOut(BaseModel):
    id: str
    human_email: str
    created_at: datetime


class ApiKeyOut(BaseModel):
    id: str
    account_id: str
    key_prefix: str
    created_at: datetime
    revoked_at: datetime | None = None
