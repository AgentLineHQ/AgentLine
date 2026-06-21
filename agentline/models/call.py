"""
AgentLine — Call & Transcript Pydantic schemas
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal


class CallRequest(BaseModel):
    agent_id: str = Field(description="ID of the AI agent making the call (e.g. 'agt_abc123')")
    to_number: str = Field(description="Destination phone number in E.164 format (e.g. '+12125551234')")
    system_prompt: str | None = Field(default=None, description="Override the agent's default system prompt for this call only")
    initial_greeting: str | None = Field(default=None, description="Override the agent's greeting for this call only")
    voice_id: str | None = Field(default=None, description="Override the TTS voice for this call: preset name (e.g. 'female-1') or Cartesia UUID")
    model_tier: Literal["turbo", "balanced", "max"] = Field(default="balanced", description="LLM model tier for this call: 'turbo', 'balanced', or 'max'")
    from_number_id: str | None = Field(default=None, description="Specific phone number ID to call from; defaults to the agent's assigned number")


class CallOut(BaseModel):
    id: str = Field(description="Unique call identifier (e.g. 'call_abc123')")
    account_id: str = Field(description="Account that owns this call")
    agent_id: str = Field(description="AI agent that handled this call")
    number_id: str | None = Field(default=None, description="Phone number used for this call")
    direction: str = Field(description="Call direction: 'inbound' or 'outbound'")
    from_number: str = Field(description="Caller phone number in E.164 format")
    to_number: str = Field(description="Destination phone number in E.164 format")
    status: str = Field(description="Call status: 'initiated', 'in-progress', 'completed', or 'failed'")
    duration_seconds: int | None = Field(default=None, description="Call duration in seconds (set after call completes)")
    transcript: list | None = Field(default=None, description="Full conversation transcript as a list of {role, text, timestamp} objects")
    started_at: datetime = Field(description="When the call was initiated")
    ended_at: datetime | None = Field(default=None, description="When the call ended (null if still in progress)")


class TranscriptEntry(BaseModel):
    role: str = Field(description="Speaker role: 'human' (caller) or 'assistant' (AI agent)")
    text: str = Field(description="What was said in this turn")
    timestamp: str = Field(description="ISO 8601 timestamp of when this turn occurred")
