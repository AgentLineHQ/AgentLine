"""
AgentLine — Email Client
- Supabase Auth OTP send/verify
- Resend transactional emails (low-balance alerts, etc.)
"""

import logging
import httpx

from agentline.config import settings

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


async def send_otp(email: str) -> dict:
    """
    Send a magic link / OTP to the given email via Supabase Auth REST API.
    Supabase handles email delivery, rate limiting, and expiry.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.SUPABASE_URL}/auth/v1/otp",
            headers={
                "apikey": settings.SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json={
                "email": email,
            },
        )
        if response.status_code >= 400:
            logger.error("Supabase OTP send failed: %s", response.text)
            raise Exception(f"Supabase OTP failed: {response.text}")

        logger.info("OTP sent to %s via Supabase", email)
        return {"success": True, "email": email}


async def send_low_balance_email(
    to_email: str,
    *,
    balance: float,
    required: float,
    account_id: str | None = None,
) -> dict | None:
    """
    Send the Resend "Low balance" template when a debit cannot be completed.

    Uses the published template id/alias from RESEND_LOW_BALANCE_TEMPLATE.
    Returns the Resend response payload on success, or None if skipped/failed.
    Never raises — billing must not fail because email delivery failed.
    """
    api_key = (settings.RESEND_API_KEY or "").strip()
    if not api_key:
        logger.warning(
            "Skipping low-balance email to %s — RESEND_API_KEY is not configured",
            to_email,
        )
        return None

    template_id = (settings.RESEND_LOW_BALANCE_TEMPLATE or "Low balance").strip()
    from_email = (settings.RESEND_FROM_EMAIL or "AgentLine <billing@agentline.cloud>").strip()

    # Template variables (keys must match the published Resend template)
    variables = {
        "BALANCE": f"{balance:.2f}",
        "REQUIRED": f"{required:.2f}",
        "CURRENCY": "USD",
    }

    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": "Low balance on your AgentLine account",
        "template": {
            "id": template_id,
            "variables": variables,
        },
        "tags": [
            {"name": "type", "value": "low_balance"},
        ],
    }
    if account_id:
        # Resend tags: only ASCII letters, numbers, underscores, dashes
        safe_id = "".join(
            c if c.isalnum() or c in "_-" else "_" for c in account_id
        )[:256]
        if safe_id:
            payload["tags"].append({"name": "account_id", "value": safe_id})

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if response.status_code >= 400:
            logger.error(
                "Resend low-balance email failed (%s): %s",
                response.status_code,
                response.text,
            )
            return None

        data = response.json()
        logger.info(
            "Low-balance email sent to %s via Resend template %r (id=%s)",
            to_email,
            template_id,
            data.get("id"),
        )
        return data
    except Exception as e:
        logger.error("Resend low-balance email error for %s: %s", to_email, e)
        return None


async def verify_otp(email: str, otp_code: str) -> dict:
    """
    Verify the OTP code via Supabase Auth REST API.
    Returns the Supabase user + session on success.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.SUPABASE_URL}/auth/v1/verify",
            headers={
                "apikey": settings.SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json={
                "email": email,
                "token": otp_code,
                "type": "email",
            },
        )
        if response.status_code >= 400:
            logger.error("Supabase OTP verify failed: %s", response.text)
            raise Exception(f"Verification failed: {response.text}")

        data = response.json()
        return {
            "user": data.get("user"),
            "session": data.get("session"),
        }
