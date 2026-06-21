"""
AgentLine — Telnyx Client
Wraps Telnyx SDK for number provisioning, SMS, and call initiation.
"""

import telnyx
from agentline.config import settings

client = telnyx.AsyncTelnyx(api_key=settings.TELNYX_API_KEY)


async def provision_number(
    country: str = "US",
    area_code: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """
    Search for and purchase a phone number from Telnyx.
    Returns dict with phone_number (E.164) and telnyx_id.
    """
    params = {
        "country_code": country,
        "features": ["sms", "voice"],
    }
    if area_code:
        params["national_destination_code"] = area_code

    numbers = await client.available_phone_numbers.list(filter=params)
    if not numbers.data:
        raise Exception(f"No numbers available in {country} {area_code or ''}")

    chosen = numbers.data[0].phone_number

    # Purchase the number and bind it to our TeXML application
    order = await client.number_orders.create(
        phone_numbers=[{"phone_number": chosen}],
        connection_id=settings.TELNYX_CONNECTION_ID,
    )

    return {
        "phone_number": chosen,
        "telnyx_id": order.data.phone_numbers[0].id if order.data and order.data.phone_numbers else "pending",
    }


async def release_number(telnyx_id: str):
    """Release a phone number back to Telnyx."""
    await client.phone_numbers.delete(telnyx_id)


async def send_sms(
    from_number: str,
    to_number: str,
    body: str,
    media_url: str | None = None,
) -> dict:
    """Send an SMS/MMS via Telnyx."""
    params = {
        "from_": from_number,
        "to": to_number,
        "text": body,
        "messaging_profile_id": settings.TELNYX_MESSAGING_PROFILE_ID,
    }
    if media_url:
        params["media_urls"] = [media_url]

    result = await client.messages.send(**params)
    
    # Safely extract status from Pydantic model response
    status = "queued"
    if result.data and result.data.to:
        to_item = result.data.to[0]
        if hasattr(to_item, "status"):
            status = to_item.status
        elif isinstance(to_item, dict):
            status = to_item.get("status", "queued")

    return {
        "telnyx_message_id": result.data.id if result.data else "unknown",
        "status": status,
    }


import base64

async def initiate_call(
    from_number: str,
    to_number: str,
    call_id: str,
) -> str:
    """
    Place an outbound call via Telnyx.
    call_id is our internal ID, passed as client_state for webhook correlation.
    Returns the Telnyx call_control_id.
    """
    encoded_state = base64.b64encode(call_id.encode('utf-8')).decode('utf-8')
    result = await client.calls.dial(
        connection_id=settings.TELNYX_CONNECTION_ID,
        from_=from_number,
        to=to_number,
        client_state=encoded_state,
        webhook_url=f"{settings.BASE_URL}/telnyx/voice",
    )
    return result.data.call_control_id if result.data else "unknown"
