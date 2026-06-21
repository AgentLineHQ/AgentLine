"""
AgentLine — Auth Middleware
Bearer token authentication using bcrypt-hashed API keys.
"""

from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import bcrypt

from agentline.database import get_db

security = HTTPBearer(
    scheme_name="API Key",
    description="Pass your API key as a Bearer token: `Authorization: Bearer sk_live_xxx`",
)


async def get_current_account(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db=Depends(get_db),
):
    """
    Validate the Bearer token against stored API key hashes.
    Returns the full account record (merged with api_keys row).
    """
    token = credentials.credentials

    if not token.startswith("sk_live_"):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key format. Keys must start with 'sk_live_'.",
        )

    prefix = token[:12]

    # Find all non-revoked keys matching this prefix
    rows = await db.fetch(
        """SELECT ak.id AS key_id, ak.key_hash, ak.key_prefix,
                  a.id, a.human_email, a.created_at
           FROM api_keys ak
           JOIN accounts a ON a.id = ak.account_id
           WHERE ak.key_prefix = $1 AND ak.revoked_at IS NULL""",
        prefix,
    )

    for row in rows:
        if bcrypt.checkpw(token.encode('utf-8'), row["key_hash"].encode('utf-8')):
            return dict(row)

    raise HTTPException(
        status_code=401,
        detail="Invalid or revoked API key.",
    )
