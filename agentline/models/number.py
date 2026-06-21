"""
AgentLine — Phone Number Pydantic schemas
"""

from pydantic import BaseModel, Field
from datetime import datetime


class NumberProvision(BaseModel):
    agent_id: str = Field(description="ID of the AI agent to assign this phone number to (e.g. 'agt_abc123')")
    country: str = Field(default="US", description="Country code for the phone number (currently only 'US' is supported)")
    number_type: str = Field(default="local", description="Type of phone number: 'local' or 'tollfree'")
    area_code: str | None = Field(default=None, description="Preferred 3-digit US area code (e.g. '212' for NYC, '415' for SF, '310' for LA)")
    pattern: str | None = Field(default=None, description="Legacy digit pattern match (prefer area_code instead)")


class NumberOut(BaseModel):
    id: str = Field(description="Unique phone number identifier (e.g. 'num_abc123')")
    account_id: str = Field(description="Account that owns this number")
    agent_id: str | None = Field(default=None, description="AI agent this number is assigned to (null if unassigned)")
    phone_number: str = Field(description="Phone number in E.164 format (e.g. '+12125551234')")
    country: str = Field(description="Country code (e.g. 'US')")
    status: str = Field(description="Number status: 'active' or 'released'")
    created_at: datetime = Field(description="When the number was provisioned")
    released_at: datetime | None = Field(default=None, description="When the number was released (null if still active)")
