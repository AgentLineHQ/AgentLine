"""
AgentLine — Supabase Auth Client
Handles OTP email sending and verification via Supabase Auth.
Uses supabase-auth (gotrue-py) directly — no heavy supabase meta-package needed.
"""

import logging
import httpx

from agentline.config import settings

logger = logging.getLogger(__name__)


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
