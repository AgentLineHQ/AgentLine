"""Twilio telephony provider.

Uses the Twilio REST API directly so the core install stays free of the
Twilio SDK. Media streams use the same JSON framing as SignalWire.
"""

import logging

import httpx

from agentline.config import settings
from agentline.database import get_db_conn
from agentline.providers.base import LamlMedia, ProvisionedNumber, SmsResult

logger = logging.getLogger(__name__)


class TwilioProvider:
    name = "twilio"
    media = "twilio"

    def __init__(self):
        self._laml = LamlMedia("twilio", media="twilio")

    def is_configured(self) -> bool:
        return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN)

    def _auth(self) -> tuple[str, str]:
        if not self.is_configured():
            raise RuntimeError("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.")
        return (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    def _base(self) -> str:
        return (
            "https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.TWILIO_ACCOUNT_SID}"
        )

    def stream_xml(self, call_id: str) -> str:
        return self._laml.stream_xml(call_id)

    def sip_dial_xml(self, sip_uri: str, headers: dict | None = None) -> str:
        return self._laml.sip_dial_xml(sip_uri, headers)

    def say_xml(self, text: str) -> str:
        return self._laml.say_xml(text)

    def _voice_form(self) -> dict:
        return {
            "VoiceUrl": self._laml.inbound_url(),
            "VoiceMethod": "POST",
            "SmsUrl": self._laml.sms_url(),
            "SmsMethod": "POST",
            "StatusCallback": self._laml.inbound_hangup_url(),
            "StatusCallbackMethod": "POST",
        }

    async def initiate_call(self, from_number: str, to_number: str, call_id: str) -> str:
        data = {
            "From": from_number,
            "To": to_number,
            "Url": self._laml.answer_url(call_id),
            "Method": "POST",
            "StatusCallback": self._laml.hangup_url(call_id),
            "StatusCallbackMethod": "POST",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self._base()}/Calls.json",
                auth=self._auth(),
                data=data,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Twilio call failed: {response.text}")
            return response.json().get("sid", "")

    async def hangup_call(self, provider_call_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base()}/Calls/{provider_call_id}.json",
                auth=self._auth(),
                data={"Status": "completed"},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Twilio hangup failed: {response.text}")

    async def send_sms(self, from_number: str, to_number: str, body: str, media_url: str | None = None) -> SmsResult:
        data = {"From": from_number, "To": to_number, "Body": body}
        if media_url:
            data["MediaUrl"] = media_url
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base()}/Messages.json",
                auth=self._auth(),
                data=data,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Twilio SMS failed: {response.text}")
            payload = response.json()
            return SmsResult(provider_message_id=payload.get("sid", ""), status=payload.get("status", "queued"))

    async def provision_number(
        self,
        country: str = "US",
        number_type: str = "local",
        area_code: str | None = None,
        pattern: str | None = None,
        agent_id: str | None = None,
    ) -> ProvisionedNumber:
        iso = (country or "US").upper()
        kind = "TollFree" if number_type == "tollfree" else "Local"
        params: dict[str, str] = {}
        if area_code:
            params["AreaCode"] = area_code
        elif pattern:
            params["Contains"] = pattern
        async with httpx.AsyncClient(timeout=15.0) as client:
            search = await client.get(
                f"{self._base()}/AvailablePhoneNumbers/{iso}/{kind}.json",
                auth=self._auth(),
                params=params,
            )
            if search.status_code >= 400:
                raise RuntimeError(f"Twilio number search failed: {search.text}")
            available = search.json().get("available_phone_numbers") or []
            if not available:
                raise RuntimeError(f"No Twilio {number_type} numbers available for {iso}.")
            chosen = available[0]["phone_number"]
            buy = await client.post(
                f"{self._base()}/IncomingPhoneNumbers.json",
                auth=self._auth(),
                data={"PhoneNumber": chosen, **self._voice_form()},
            )
            if buy.status_code >= 400:
                raise RuntimeError(f"Twilio number purchase failed: {buy.text}")
            payload = buy.json()
            return ProvisionedNumber(
                provider_id=payload.get("sid", chosen),
                phone_number=payload.get("phone_number", chosen),
            )

    async def release_number(self, provider_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(
                f"{self._base()}/IncomingPhoneNumbers/{provider_id}.json",
                auth=self._auth(),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Twilio number release failed: {response.text}")

    async def configure_number(self, provider_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base()}/IncomingPhoneNumbers/{provider_id}.json",
                auth=self._auth(),
                data=self._voice_form(),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Twilio webhook update failed: {response.text}")

    async def set_status_callback(self, provider_call_id: str, call_id: str) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{self._base()}/Calls/{provider_call_id}.json",
                auth=self._auth(),
                data={
                    "StatusCallback": self._laml.hangup_url(call_id),
                    "StatusCallbackMethod": "POST",
                },
            )
            response.raise_for_status()

    async def reconfigure_active_numbers(self) -> None:
        if not self.is_configured():
            logger.info("Twilio is not configured — skipping number webhook refresh.")
            return
        async with get_db_conn() as db:
            rows = await db.fetch(
                "SELECT phone_number, provider_id, provider FROM phone_numbers WHERE status = 'active'"
            )
        for row in rows:
            owner = row["provider"] or self.name
            if owner != self.name:
                continue
            try:
                await self.configure_number(row["provider_id"])
                logger.info("Refreshed Twilio webhooks for %s", row["phone_number"])
            except Exception as exc:
                logger.warning("Twilio webhook refresh failed for %s: %s", row["phone_number"], exc)
