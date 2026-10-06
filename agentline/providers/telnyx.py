"""Telnyx telephony provider.

Calls use the TeXML API, which speaks the same media-stream protocol as
Twilio. Number search and SMS use the Telnyx v2 JSON API. No Telnyx SDK.
"""

import logging

import httpx

from agentline.config import settings
from agentline.database import get_db_conn
from agentline.providers.base import LamlMedia, ProvisionedNumber, SmsResult, xml_escape

logger = logging.getLogger(__name__)


class TelnyxProvider:
    name = "telnyx"
    media = "telnyx"

    def __init__(self):
        self._laml = LamlMedia("telnyx", media="telnyx")

    def is_configured(self) -> bool:
        return bool(settings.TELNYX_API_KEY and settings.TELNYX_ACCOUNT_SID)

    def _headers(self) -> dict[str, str]:
        if not settings.TELNYX_API_KEY:
            raise RuntimeError("Set TELNYX_API_KEY.")
        return {
            "Authorization": f"Bearer {settings.TELNYX_API_KEY}",
            "Content-Type": "application/json",
        }

    def _texml(self) -> str:
        if not settings.TELNYX_ACCOUNT_SID:
            raise RuntimeError("Set TELNYX_ACCOUNT_SID to the TeXML application id.")
        return f"https://api.telnyx.com/v2/texml/Accounts/{settings.TELNYX_ACCOUNT_SID}"

    def stream_xml(self, call_id: str) -> str:
        from agentline.providers.base import public_ws_url

        stream_url = public_ws_url(f"/telnyx/stream/{call_id}")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{xml_escape(stream_url)}" bidirectionalMode="rtp" />
    </Connect>
</Response>"""

    def sip_dial_xml(self, sip_uri: str, headers: dict | None = None) -> str:
        return self._laml.sip_dial_xml(sip_uri, headers)

    def say_xml(self, text: str) -> str:
        return self._laml.say_xml(text)

    async def initiate_call(self, from_number: str, to_number: str, call_id: str) -> str:
        data = {
            "To": to_number,
            "From": from_number,
            "Url": self._laml.answer_url(call_id),
            "Method": "POST",
            "StatusCallback": self._laml.hangup_url(call_id),
            "StatusCallbackMethod": "POST",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self._texml()}/Calls",
                headers={"Authorization": f"Bearer {settings.TELNYX_API_KEY}"},
                data=data,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Telnyx call failed: {response.text}")
            payload = response.json()
            return payload.get("sid") or payload.get("call_sid") or payload.get("data", {}).get("call_sid", "")

    async def hangup_call(self, provider_call_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._texml()}/Calls/{provider_call_id}",
                headers={"Authorization": f"Bearer {settings.TELNYX_API_KEY}"},
                data={"Status": "completed"},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Telnyx hangup failed: {response.text}")

    async def send_sms(self, from_number: str, to_number: str, body: str, media_url: str | None = None) -> SmsResult:
        payload: dict = {"from": from_number, "to": to_number, "text": body}
        if media_url:
            payload["media_urls"] = [media_url]
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.telnyx.com/v2/messages",
                headers=self._headers(),
                json=payload,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Telnyx SMS failed: {response.text}")
            data = response.json().get("data") or {}
            return SmsResult(provider_message_id=data.get("id", ""), status=data.get("to", [{}])[0].get("status", "queued") if isinstance(data.get("to"), list) else "queued")

    async def provision_number(
        self,
        country: str = "US",
        number_type: str = "local",
        area_code: str | None = None,
        pattern: str | None = None,
        agent_id: str | None = None,
    ) -> ProvisionedNumber:
        params = {
            "filter[country_code]": (country or "US").upper(),
            "filter[limit]": "1",
            "filter[phone_number_type]": "toll_free" if number_type == "tollfree" else "local",
        }
        if area_code:
            params["filter[national_destination_code]"] = area_code
        elif pattern:
            params["filter[phone_number][contains]"] = pattern
        async with httpx.AsyncClient(timeout=15.0) as client:
            search = await client.get(
                "https://api.telnyx.com/v2/available_phone_numbers",
                headers=self._headers(),
                params=params,
            )
            if search.status_code >= 400:
                raise RuntimeError(f"Telnyx number search failed: {search.text}")
            available = search.json().get("data") or []
            if not available:
                raise RuntimeError(f"No Telnyx numbers available for {params['filter[country_code]']}.")
            chosen = available[0].get("phone_number")
            order_body: dict = {"phone_numbers": [{"phone_number": chosen}]}
            if settings.TELNYX_CONNECTION_ID:
                order_body["connection_id"] = settings.TELNYX_CONNECTION_ID
            buy = await client.post(
                "https://api.telnyx.com/v2/number_orders",
                headers=self._headers(),
                json=order_body,
            )
            if buy.status_code >= 400:
                raise RuntimeError(f"Telnyx number order failed: {buy.text}")
            order = buy.json().get("data") or {}
            numbers = order.get("phone_numbers") or []
            provider_id = chosen
            if numbers and isinstance(numbers[0], dict):
                provider_id = numbers[0].get("id") or numbers[0].get("phone_number") or chosen
            return ProvisionedNumber(provider_id=provider_id, phone_number=chosen)

    async def release_number(self, provider_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(
                f"https://api.telnyx.com/v2/phone_numbers/{provider_id}",
                headers=self._headers(),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Telnyx number release failed: {response.text}")

    async def configure_number(self, provider_id: str) -> None:
        if not settings.TELNYX_CONNECTION_ID:
            logger.info(
                "TELNYX_CONNECTION_ID is empty — set the TeXML app voice URL to %s.",
                self._laml.inbound_url(),
            )
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.patch(
                f"https://api.telnyx.com/v2/phone_numbers/{provider_id}",
                headers=self._headers(),
                json={"connection_id": settings.TELNYX_CONNECTION_ID},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Telnyx number update failed: {response.text}")

    async def set_status_callback(self, provider_call_id: str, call_id: str) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{self._texml()}/Calls/{provider_call_id}",
                headers={"Authorization": f"Bearer {settings.TELNYX_API_KEY}"},
                data={
                    "StatusCallback": self._laml.hangup_url(call_id),
                    "StatusCallbackMethod": "POST",
                },
            )
            response.raise_for_status()

    async def reconfigure_active_numbers(self) -> None:
        if not self.is_configured():
            logger.info("Telnyx is not configured — skipping number webhook refresh.")
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
            except Exception as exc:
                logger.warning("Telnyx webhook refresh failed for %s: %s", row["phone_number"], exc)
