"""SignalWire telephony provider.

Wraps the LaML and Relay REST calls in ``agentline.signalwire_client``.
"""

import logging

import httpx

from agentline.config import settings
from agentline.database import get_db_conn
from agentline.providers.base import LamlMedia, ProvisionedNumber, SmsResult
from agentline.signalwire_client import (
    configure_number_webhooks,
    hangup_call,
    initiate_call,
    provision_number,
    release_number,
    send_sms,
)

logger = logging.getLogger(__name__)


class SignalWireProvider:
    name = "signalwire"
    media = "signalwire"

    def __init__(self):
        self._laml = LamlMedia("signalwire", media="signalwire")

    def is_configured(self) -> bool:
        return bool(
            settings.SIGNALWIRE_PROJECT_ID
            and settings.SIGNALWIRE_TOKEN
            and settings.SIGNALWIRE_SPACE_URL
        )

    def stream_xml(self, call_id: str) -> str:
        return self._laml.stream_xml(call_id)

    def sip_dial_xml(self, sip_uri: str, headers: dict | None = None) -> str:
        return self._laml.sip_dial_xml(sip_uri, headers)

    def say_xml(self, text: str) -> str:
        return self._laml.say_xml(text)

    async def initiate_call(self, from_number: str, to_number: str, call_id: str) -> str:
        return await initiate_call(
            from_number=from_number,
            to_number=to_number,
            call_id=call_id,
            answer_url=self._laml.answer_url(call_id),
            status_url=self._laml.hangup_url(call_id),
        )

    async def hangup_call(self, provider_call_id: str) -> None:
        await hangup_call(provider_call_id)

    async def send_sms(self, from_number: str, to_number: str, body: str, media_url: str | None = None) -> SmsResult:
        result = await send_sms(from_number, to_number, body, media_url)
        return SmsResult(
            provider_message_id=result.get("provider_message_id", ""),
            status=result.get("status", "queued"),
        )

    async def provision_number(
        self,
        country: str = "US",
        number_type: str = "local",
        area_code: str | None = None,
        pattern: str | None = None,
        agent_id: str | None = None,
    ) -> ProvisionedNumber:
        data = await provision_number(
            country=country,
            number_type=number_type,
            area_code=area_code,
            pattern=pattern,
            agent_id=agent_id,
        )
        return ProvisionedNumber(
            provider_id=data["provider_id"],
            phone_number=data["phone_number"],
        )

    async def release_number(self, provider_id: str) -> None:
        await release_number(provider_id)

    async def configure_number(self, provider_id: str) -> None:
        await configure_number_webhooks(provider_id)

    async def set_status_callback(self, provider_call_id: str, call_id: str) -> None:
        space = settings.SIGNALWIRE_SPACE_URL
        project = settings.SIGNALWIRE_PROJECT_ID
        url = (
            f"https://{space}/api/laml/2010-04-01/Accounts/{project}"
            f"/Calls/{provider_call_id}.json"
        )
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                url,
                auth=(project, settings.SIGNALWIRE_TOKEN),
                data={
                    "StatusCallback": self._laml.hangup_url(call_id),
                    "StatusCallbackMethod": "POST",
                },
            )
            response.raise_for_status()

    async def reconfigure_active_numbers(self) -> None:
        if not self.is_configured():
            logger.info("SignalWire is not configured — skipping number webhook refresh.")
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
                logger.info("Refreshed webhooks for %s", row["phone_number"])
            except Exception as exc:
                logger.warning("Webhook refresh failed for %s: %s", row["phone_number"], exc)
