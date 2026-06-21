"""
AgentLine — Voice Settings Router
Endpoints for managing voice preferences at the account level,
and listing available voice presets.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agentline.auth_middleware import get_current_account
from agentline.database import get_db
from agentline.voice.voices import (
    resolve_voice_id,
    list_available_voices,
    DEFAULT_VOICE_ID,
    VOICE_PRESETS,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Voice"])


# ── Request/Response Models ───────────────────────────────────

class VoiceSettingUpdate(BaseModel):
    """Set account-level default voice."""
    voice_id: str = Field(description="TTS voice preset name (e.g. 'female-1', 'male-1') or Cartesia voice UUID")


class VoiceSettingOut(BaseModel):
    """Current voice settings for the account."""
    account_id: str
    default_voice_id: str | None
    resolved_voice_id: str  # What will actually be used (after resolution)


# ── Endpoints ─────────────────────────────────────────────────

@router.get("/v1/voices", operation_id="list_available_voices")
async def get_available_voices():
    """
    List all available voice presets for AI phone agents.

    Returns named TTS (text-to-speech) voice presets that can be used
    when configuring AI agents or making phone calls. Each voice defines
    how your AI agent sounds on the phone.

    You can use a preset name (e.g. "female-1", "male-1") or pass any
    valid Cartesia voice UUID directly as a voice_id.
    """
    return {
        "voices": list_available_voices(),
        "default_voice_id": DEFAULT_VOICE_ID,
        "usage_hint": (
            "Pass a preset name (e.g. 'female-1') or a Cartesia UUID as voice_id "
            "when creating/updating agents or making calls."
        ),
    }


@router.get("/v1/account/voice", operation_id="get_account_voice")
async def get_account_voice(
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Get the current account-level default voice for AI phone agents.

    Returns which voice is used for all AI agents under this account
    unless overridden at the agent level or per-call. Controls how
    your AI agents sound during phone conversations.
    """
    row = await db.fetchrow(
        "SELECT default_voice_id FROM accounts WHERE id = $1",
        account["id"],
    )
    raw_voice = row["default_voice_id"] if row else None

    return VoiceSettingOut(
        account_id=account["id"],
        default_voice_id=raw_voice,
        resolved_voice_id=resolve_voice_id(raw_voice),
    )


@router.patch("/v1/account/voice", operation_id="set_account_voice")
async def update_account_voice(
    body: VoiceSettingUpdate,
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Set the account-level default voice for AI phone agents.

    This becomes the default voice for ALL AI agents under this account,
    controlling how they sound on phone calls. Individual agents or
    specific calls can still override this setting.

    Voice resolution priority:
      1. Per-call voice_id (POST /v1/calls)
      2. Agent voice_id (PATCH /v1/agents/{id})
      3. Account default (this endpoint)  ← you are here
      4. System default (Barbershop Man)

    Accepts:
      - A preset name: "female-1", "female-2", "male-1"
      - A Cartesia voice UUID: "e07c00bc-4134-4eae-9ea4-1a55fb45746b"
    """
    # Validate the voice_id resolves to something valid
    resolved = resolve_voice_id(body.voice_id)

    await db.execute(
        "UPDATE accounts SET default_voice_id = $1 WHERE id = $2",
        body.voice_id,  # Store the raw value (preset name or UUID)
        account["id"],
    )

    logger.info(
        "Account %s — set default voice to '%s' (resolves to %s)",
        account["id"], body.voice_id, resolved,
    )

    return VoiceSettingOut(
        account_id=account["id"],
        default_voice_id=body.voice_id,
        resolved_voice_id=resolved,
    )


@router.delete("/v1/account/voice", operation_id="reset_account_voice")
async def reset_account_voice(
    account=Depends(get_current_account),
    db=Depends(get_db),
):
    """
    Reset account voice to the system default.

    Removes the account-level voice override so all AI agents fall back
    to the system default voice during phone calls (unless they have
    their own voice_id configured).
    """
    await db.execute(
        "UPDATE accounts SET default_voice_id = NULL WHERE id = $1",
        account["id"],
    )

    logger.info("Account %s — reset default voice to system default", account["id"])

    return {
        "account_id": account["id"],
        "default_voice_id": None,
        "resolved_voice_id": DEFAULT_VOICE_ID,
        "message": "Account voice reset to system default.",
    }
