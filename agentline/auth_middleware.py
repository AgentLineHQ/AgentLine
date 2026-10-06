"""
AgentLine — Auth Middleware
Bearer token authentication using bcrypt-hashed API keys.
"""

from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import asyncio
import bcrypt

from agentline.database import get_db

security = HTTPBearer(
    scheme_name="API Key",
    description="Pass your API key as a Bearer token: `Authorization: Bearer al_live_xxx`",
)

# Accepted key prefixes. `al_live_` is the current mint prefix; `sk_live_` is
# legacy (collided with Stripe secret-key redaction in agent runtimes).
_VALID_KEY_PREFIXES = ("al_live_", "sk_live_")


async def resolve_account(token: str, db) -> dict | None:
    """
    Validate a Bearer API key against stored hashes.
    Returns the account record on success, or None on any failure.
    Never raises — callers (WebSocket / push-token routes) decide how to react.
    """
    if not token or not token.startswith(_VALID_KEY_PREFIXES):
        return None

    prefix = token[:12]
    rows = await db.fetch(
        """SELECT ak.id AS key_id, ak.key_hash, ak.key_prefix,
                  a.id, a.human_email, a.created_at
           FROM api_keys ak
           JOIN accounts a ON a.id = ak.account_id
           WHERE ak.key_prefix = $1 AND ak.revoked_at IS NULL""",
        prefix,
    )
    for row in rows:
        # bcrypt is CPU-bound (~50-250ms) — run off the event loop so
        # concurrent requests (incl. live voice WebSockets) never stall.
        ok = await asyncio.to_thread(
            bcrypt.checkpw,
            token.encode("utf-8"),
            row["key_hash"].encode("utf-8"),
        )
        if ok:
            return dict(row)
    return None


async def get_current_account(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db=Depends(get_db),
):
    """
    Validate the Bearer token against stored API key hashes.
    Returns the full account record (merged with api_keys row).

    Accepts both `al_live_` (current) and `sk_live_` (legacy) key prefixes.
    """
    account = await resolve_account(credentials.credentials, db)
    if account is None:
        token = credentials.credentials or ""
        if not token.startswith(_VALID_KEY_PREFIXES):
            raise HTTPException(
                status_code=401,
                detail="Invalid API key format. Keys must start with 'al_live_' or 'sk_live_'.",
            )
        raise HTTPException(
            status_code=401,
            detail="Invalid or revoked API key.",
        )
    return account
