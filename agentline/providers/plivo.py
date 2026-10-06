"""Plivo telephony provider.

Plivo's answer XML and media websocket differ from LaML. The built-in voice
pipeline already knows how to send audio back with the ``plivo`` framing.
"""

import logging

import httpx

from agentline.config import settings
from agentline.database import get_db_conn
from agentline.providers.base import (
    ProvisionedNumber,
    SmsResult,
    public_http_url,
    public_ws_url,
    xml_escape,
)

logger = logging.getLogger(__name__)


class PlivoProvider:
    name = "plivo"
    media = "plivo"

    def is_configured(self) -> bool:
        return bool(settings.PLIVO_AUTH_ID and settings.PLIVO_AUTH_TOKEN)

    def _auth(self) -> tuple[str, str]:
        if not self.is_configured():
            raise RuntimeError("Set PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN.")
        return (settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN)

    def _base(self) -> str:
        return f"https://api.plivo.com/v1/Account/{settings.PLIVO_AUTH_ID}"

    def answer_url(self, call_id: str) -> str:
        return public_http_url(f"/plivo/answer/{call_id}")

    def hangup_url(self, call_id: str) -> str:
        return public_http_url(f"/plivo/hangup/{call_id}")

    def stream_xml(self, call_id: str) -> str:
        url = public_ws_url(f"/plivo/stream/{call_id}")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">{xml_escape(url)}</Stream>
</Response>"""

    def sip_dial_xml(self, sip_uri: str, headers: dict | None = None) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <User>{xml_escape(sip_uri)}</User>
    </Dial>
</Response>"""

    def say_xml(self, text: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Speak>{xml_escape(text)}</Speak></Response>"
        )

    async def initiate_call(self, from_number: str, to_number: str, call_id: str) -> str:
        payload = {
            "from": from_number,
            "to": to_number,
            "answer_url": self.answer_url(call_id),
            "answer_method": "POST",
            "hangup_url": self.hangup_url(call_id),
            "hangup_method": "POST",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self._base()}/Call/",
                auth=self._auth(),
                json=payload,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Plivo call failed: {response.text}")
            data = response.json()
            return data.get("request_uuid") or data.get("call_uuid") or ""

    async def hangup_call(self, provider_call_id: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(
                f"{self._base()}/Call/{provider_call_id}/",
                auth=self._auth(),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Plivo hangup failed: {response.text}")

    async def send_sms(self, from_number: str, to_number: str, body: str, media_url: str | None = None) -> SmsResult:
        payload = {"src": from_number, "dst": to_number, "text": body}
        if media_url:
            payload["media_urls"] = [media_url]
            url = f"{self._base()}/Message/"
        else:
            url = f"{self._base()}/Message/"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, auth=self._auth(), json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"Plivo SMS failed: {response.text}")
            data = response.json()
            message_uuid = data.get("message_uuid") or []
            message_id = message_uuid[0] if isinstance(message_uuid, list) and message_uuid else str(message_uuid)
            return SmsResult(provider_message_id=message_id, status="queued")

    async def provision_number(
        self,
        country: str = "US",
        number_type: str = "local",
        area_code: str | None = None,
        pattern: str | None = None,
        agent_id: str | None = None,
    ) -> ProvisionedNumber:
        params = {
            "country_iso": (country or "US").upper(),
            "type": "tollfree" if number_type == "tollfree" else "local",
            "services": "voice,sms",
        }
        if area_code:
            params["pattern"] = area_code
        elif pattern:
            params["pattern"] = pattern
        async with httpx.AsyncClient(timeout=15.0) as client:
            search = await client.get(
                f"{self._base()}/PhoneNumber/",
                auth=self._auth(),
                params=params,
            )
            if search.status_code >= 400:
                raise RuntimeError(f"Plivo number search failed: {search.text}")
            numbers = search.json().get("objects") or []
            if not numbers:
                raise RuntimeError(f"No Plivo numbers available for {params['country_iso']}.")
            chosen = numbers[0].get("number")
            buy = await client.post(
                f"{self._base()}/PhoneNumber/{chosen}/",
                auth=self._auth(),
                json={"app_id": settings.PLIVO_APP_ID} if settings.PLIVO_APP_ID else {},
            )
            if buy.status_code >= 400:
                raise RuntimeError(f"Plivo number purchase failed: {buy.text}")
            e164 = chosen if str(chosen).startswith("+") else f"+{chosen}"
            return ProvisionedNumber(provider_id=str(chosen), phone_number=e164)

    async def release_number(self, provider_id: str) -> None:
        number = provider_id.lstrip("+")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(
                f"{self._base()}/PhoneNumber/{number}/",
                auth=self._auth(),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Plivo number release failed: {response.text}")

    async def configure_number(self, provider_id: str) -> None:
        if not settings.PLIVO_APP_ID:
            logger.info(
                "PLIVO_APP_ID is empty — point a Plivo application at %s and %s.",
                public_http_url("/plivo/inbound"),
                public_http_url("/plivo/hangup/inbound"),
            )
            return
        number = provider_id.lstrip("+")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base()}/PhoneNumber/{number}/",
                auth=self._auth(),
                json={"app_id": settings.PLIVO_APP_ID},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Plivo number update failed: {response.text}")

    async def set_status_callback(self, provider_call_id: str, call_id: str) -> None:
        # Plivo takes hangup_url when the call is created. Nothing to patch.
        return None

    async def reconfigure_active_numbers(self) -> None:
        if not self.is_configured():
            logger.info("Plivo is not configured — skipping number webhook refresh.")
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
                logger.warning("Plivo webhook refresh failed for %s: %s", row["phone_number"], exc)
