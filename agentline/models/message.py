"""
AgentLine — Message & Conversation Pydantic schemas
"""

from pydantic import BaseModel, Field
from datetime import datetime


class MessageSend(BaseModel):
    agent_id: str = Field(description="ID of the AI agent sending the SMS (e.g. 'agt_abc123')")
    to_number: str = Field(description="Destination phone number in E.164 format (e.g. '+12125551234')")
    body: str = Field(description="SMS message text content")
    media_url: str | None = Field(default=None, description="URL of media to attach (MMS)")
    from_number_id: str | None = Field(default=None, description="Specific phone number ID to send from; defaults to the agent's assigned number")


class MessageOut(BaseModel):
    id: str = Field(description="Unique message identifier (e.g. 'msg_abc123')")
    account_id: str = Field(description="Account that owns this message")
    agent_id: str | None = Field(default=None, description="AI agent that sent or received this message")
    number_id: str | None = Field(default=None, description="Phone number used for this message")
    conversation_id: str | None = Field(default=None, description="Conversation thread this message belongs to")
    direction: str = Field(description="Message direction: 'inbound' or 'outbound'")
    from_number: str = Field(description="Sender phone number in E.164 format")
    to_number: str = Field(description="Recipient phone number in E.164 format")
    body: str | None = Field(default=None, description="SMS message text content")
    media_url: str | None = Field(default=None, description="URL of attached media (MMS)")
    status: str = Field(description="Delivery status: 'sent', 'delivered', 'failed'")
    created_at: datetime = Field(description="When the message was sent or received")


class ConversationOut(BaseModel):
    id: str = Field(description="Unique conversation identifier (e.g. 'conv_abc123')")
    account_id: str = Field(description="Account that owns this conversation")
    agent_id: str | None = Field(default=None, description="AI agent handling this conversation")
    number_id: str | None = Field(default=None, description="Phone number used in this conversation")
    contact_number: str = Field(description="External contact's phone number in E.164 format")
    created_at: datetime = Field(description="When the conversation started")
    last_message_at: datetime | None = Field(default=None, description="When the most recent message was exchanged")
