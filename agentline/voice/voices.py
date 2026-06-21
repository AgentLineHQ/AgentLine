"""
AgentLine — Voice Catalog
Maps human-readable voice names to Cartesia voice IDs.

Voice Resolution Order (highest priority wins):
  1. Per-call override  → POST /v1/calls { "voice_id": "..." }
  2. Agent setting       → agent.voice_id (set via PATCH /v1/agents/{id})
  3. Account default     → account.default_voice_id (set via PATCH /v1/account/voice)
  4. System default      → DEFAULT_VOICE_ID (Barbershop Man)

Users can pass either:
  - A Cartesia UUID directly (e.g. "e07c00bc-4134-4eae-9ea4-1a55fb45746b")
  - A named preset (e.g. "female-1", "male-1")
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── Voice Presets ──────────────────────────────────────────────
# Named shortcuts that map to Cartesia voice UUIDs.
# Users can reference these by name instead of memorizing UUIDs.

VOICE_PRESETS: dict[str, dict] = {
    # Female voices
    "female-1": {
        "id": "e07c00bc-4134-4eae-9ea4-1a55fb45746b",
        "name": "Female Voice 1",
        "description": "Clear, professional female voice",
    },
    "female-2": {
        "id": "630ed21c-2c5c-41cf-9d82-10a7fd668370",
        "name": "Female Voice 2",
        "description": "Warm, conversational female voice",
    },
    # Male voices
    "male-1": {
        "id": "a0e99841-438c-4a64-b679-ae501e7d6091",
        "name": "Barbershop Man",
        "description": "Clear male voice, good for phone calls",
    },
}

# System-wide default (Barbershop Man)
DEFAULT_VOICE_ID = "a0e99841-438c-4a64-b679-ae501e7d6091"

# UUID regex for validation
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def resolve_voice_id(voice_id: str | None) -> str:
    """
    Resolve a voice identifier to a Cartesia UUID.

    Accepts:
      - A valid Cartesia UUID  → returned as-is
      - A preset name ("female-1", "male-1")  → looked up in VOICE_PRESETS
      - None or invalid  → falls back to DEFAULT_VOICE_ID

    Returns:
        A valid Cartesia voice UUID string.
    """
    if not voice_id:
        return DEFAULT_VOICE_ID

    # Check if it's a named preset (case-insensitive)
    preset_key = voice_id.strip().lower()
    if preset_key in VOICE_PRESETS:
        resolved = VOICE_PRESETS[preset_key]["id"]
        logger.debug("Resolved voice preset '%s' → %s", voice_id, resolved)
        return resolved

    # Check if it's a valid UUID
    if _UUID_RE.match(voice_id.strip()):
        return voice_id.strip()

    # Legacy string values (from old schema default "cartesia-sonic-english")
    # These aren't valid Cartesia voice IDs — fall back to default
    logger.warning(
        "Invalid voice_id '%s' (not a UUID or known preset) — using default %s",
        voice_id, DEFAULT_VOICE_ID,
    )
    return DEFAULT_VOICE_ID


def resolve_voice_chain(
    per_call_voice: str | None,
    agent_voice: str | None,
    account_voice: str | None,
) -> str:
    """
    Resolve voice using the priority chain:
      per-call override > agent setting > account default > system default

    Each level can be None to fall through to the next.
    """
    # Try per-call override first
    if per_call_voice:
        resolved = resolve_voice_id(per_call_voice)
        if resolved != DEFAULT_VOICE_ID or _is_explicit_default(per_call_voice):
            logger.info("Voice: using per-call override → %s", resolved)
            return resolved

    # Try agent-level setting
    if agent_voice:
        resolved = resolve_voice_id(agent_voice)
        if resolved != DEFAULT_VOICE_ID or _is_explicit_default(agent_voice):
            logger.info("Voice: using agent setting → %s", resolved)
            return resolved

    # Try account-level default
    if account_voice:
        resolved = resolve_voice_id(account_voice)
        if resolved != DEFAULT_VOICE_ID or _is_explicit_default(account_voice):
            logger.info("Voice: using account default → %s", resolved)
            return resolved

    # System default
    logger.debug("Voice: using system default → %s", DEFAULT_VOICE_ID)
    return DEFAULT_VOICE_ID


def _is_explicit_default(voice_id: str | None) -> bool:
    """Check if the voice_id explicitly points to the default voice."""
    if not voice_id:
        return False
    resolved = voice_id.strip().lower()
    return (
        resolved == DEFAULT_VOICE_ID.lower()
        or resolved == "male-1"
    )


def list_available_voices() -> list[dict]:
    """Return all available voice presets for the /v1/voices endpoint."""
    return [
        {
            "preset_name": name,
            "voice_id": info["id"],
            "name": info["name"],
            "description": info["description"],
        }
        for name, info in VOICE_PRESETS.items()
    ]
