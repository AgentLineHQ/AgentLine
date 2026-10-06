"""Carrier webhooks for Twilio, Telnyx, and Plivo.

SignalWire keeps its own router in ``signalwire_events`` so owner mode,
relay, DTMF, and the built-in media pipeline stay on that path. These
routes stay mounted even when another carrier is the default, so a number
bought earlier keeps receiving calls. The voice runtime is chosen per call.
"""

import asyncio
import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from agentline.call_flow import (
    finalize_call,
    mark_answered,
    open_inbound_call,
    parse_duration,
    render_answer,
    run_media,
    store_inbound_sms,
)
from agentline.providers.registry import get_provider

logger = logging.getLogger(__name__)

WEBHOOK_PROVIDERS = ("twilio", "telnyx", "plivo")


def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def webhook_operation_ids() -> list[str]:
    actions = ("answer", "hangup", "inbound", "inbound_hangup", "sms", "stream")
    return [f"{name}_{action}" for name in WEBHOOK_PROVIDERS for action in actions]


def build_laml_router(provider_name: str) -> APIRouter:
    """Twilio-shaped form posts: SignalWire, Twilio, and Telnyx TeXML."""
    router = APIRouter(prefix=f"/{provider_name}", tags=[f"{provider_name} events"])

    def provider():
        return get_provider(provider_name)

    async def answer(request: Request, call_id: str):
        form = await request.form()
        call_sid = str(form.get("CallSid", "") or "")
        logger.info("%s call %s answered (sid %s)", provider_name, call_id, call_sid)
        await mark_answered(call_id, call_sid)
        try:
            xml = await render_answer(provider(), call_id)
        except Exception as exc:
            logger.error("%s answer failed for %s: %s", provider_name, call_id, exc)
            xml = provider().say_xml("This agent is not available right now. Goodbye.")
        return _xml(xml)

    async def hangup(request: Request, call_id: str):
        form = await request.form()
        await finalize_call(
            call_id,
            str(form.get("CallSid", "") or ""),
            str(form.get("CallStatus", "") or ""),
            parse_duration(form.get("CallDuration", form.get("Duration", "0"))),
        )
        return _xml("<Response/>")

    async def inbound(request: Request):
        form = await request.form()
        from_number = str(form.get("From", "") or "")
        to_number = str(form.get("To", "") or "")
        call_sid = str(form.get("CallSid", "") or "")
        logger.info("%s inbound %s -> %s", provider_name, from_number, to_number)
        call_id = await open_inbound_call(from_number, to_number, call_sid, provider_name)
        if not call_id:
            return _xml(provider().say_xml("This number is not configured. Goodbye."))
        if call_sid:
            async def _set_status():
                try:
                    await provider().set_status_callback(call_sid, call_id)
                except Exception as exc:
                    logger.warning("Status callback update failed for %s: %s", call_id, exc)
            asyncio.create_task(_set_status())
        try:
            xml = await render_answer(provider(), call_id)
        except Exception as exc:
            logger.error("%s inbound answer failed for %s: %s", provider_name, call_id, exc)
            xml = provider().say_xml("This agent is not available right now. Goodbye.")
        return _xml(xml)

    async def inbound_hangup(request: Request):
        form = await request.form()
        await finalize_call(
            None,
            str(form.get("CallSid", "") or ""),
            str(form.get("CallStatus", "") or ""),
            parse_duration(form.get("CallDuration", form.get("Duration", "0"))),
        )
        return _xml("<Response/>")

    async def sms(request: Request):
        form = await request.form()
        num_media = int(str(form.get("NumMedia", "0") or "0") or "0")
        media_url = str(form.get("MediaUrl0", "") or "") if num_media else ""
        await store_inbound_sms(
            str(form.get("From", "") or ""),
            str(form.get("To", "") or ""),
            str(form.get("Body", "") or ""),
            str(form.get("MessageSid", "") or ""),
            media_url or None,
        )
        return _xml("<Response/>")

    async def stream(websocket: WebSocket, call_id: str):
        await websocket.accept()
        logger.info("%s media stream connected for %s", provider_name, call_id)
        try:
            await run_media(websocket, call_id, provider().media)
        except WebSocketDisconnect:
            logger.info("%s media stream disconnected for %s", provider_name, call_id)
        except Exception as exc:
            logger.error("%s media stream failed for %s: %s", provider_name, call_id, exc)

    answer.__name__ = f"{provider_name}_answer"
    hangup.__name__ = f"{provider_name}_hangup"
    inbound.__name__ = f"{provider_name}_inbound"
    inbound_hangup.__name__ = f"{provider_name}_inbound_hangup"
    sms.__name__ = f"{provider_name}_sms"
    stream.__name__ = f"{provider_name}_stream"

    router.add_api_route("/answer/{call_id}", answer, methods=["POST"], operation_id=answer.__name__)
    router.add_api_route("/hangup/{call_id}", hangup, methods=["POST"], operation_id=hangup.__name__)
    router.add_api_route("/inbound", inbound, methods=["POST"], operation_id=inbound.__name__)
    router.add_api_route("/inbound_hangup", inbound_hangup, methods=["POST"], operation_id=inbound_hangup.__name__)
    router.add_api_route("/sms", sms, methods=["POST"], operation_id=sms.__name__)
    router.add_api_websocket_route("/stream/{call_id}", stream, name=stream.__name__)
    return router


def build_plivo_router() -> APIRouter:
    provider_name = "plivo"
    router = APIRouter(prefix="/plivo", tags=["plivo events"])

    def provider():
        return get_provider(provider_name)

    async def answer(request: Request, call_id: str):
        form = await request.form()
        call_uuid = str(form.get("CallUUID", "") or form.get("RequestUUID", "") or "")
        logger.info("plivo call %s answered (uuid %s)", call_id, call_uuid)
        await mark_answered(call_id, call_uuid)
        try:
            xml = await render_answer(provider(), call_id)
        except Exception as exc:
            logger.error("plivo answer failed for %s: %s", call_id, exc)
            xml = provider().say_xml("This agent is not available right now. Goodbye.")
        return _xml(xml)

    async def hangup(request: Request, call_id: str):
        form = await request.form()
        await finalize_call(
            call_id,
            str(form.get("CallUUID", "") or ""),
            str(form.get("CallStatus", "") or form.get("HangupCause", "") or ""),
            parse_duration(form.get("Duration", form.get("BillDuration", "0"))),
        )
        return _xml("<Response/>")

    async def inbound(request: Request):
        form = await request.form()
        from_number = str(form.get("From", "") or "")
        to_number = str(form.get("To", "") or "")
        call_uuid = str(form.get("CallUUID", "") or "")
        call_id = await open_inbound_call(from_number, to_number, call_uuid, provider_name)
        if not call_id:
            return _xml(provider().say_xml("This number is not configured. Goodbye."))
        try:
            xml = await render_answer(provider(), call_id)
        except Exception as exc:
            logger.error("plivo inbound answer failed for %s: %s", call_id, exc)
            xml = provider().say_xml("This agent is not available right now. Goodbye.")
        return _xml(xml)

    async def inbound_hangup(request: Request):
        form = await request.form()
        await finalize_call(
            None,
            str(form.get("CallUUID", "") or ""),
            str(form.get("CallStatus", "") or ""),
            parse_duration(form.get("Duration", "0")),
        )
        return _xml("<Response/>")

    async def sms(request: Request):
        form = await request.form()
        await store_inbound_sms(
            str(form.get("From", "") or ""),
            str(form.get("To", "") or ""),
            str(form.get("Text", "") or form.get("Body", "") or ""),
            str(form.get("MessageUUID", "") or ""),
            str(form.get("Media0", "") or "") or None,
        )
        return _xml("<Response/>")

    async def stream(websocket: WebSocket, call_id: str):
        await websocket.accept()
        try:
            await run_media(websocket, call_id, "plivo")
        except WebSocketDisconnect:
            logger.info("plivo media stream disconnected for %s", call_id)
        except Exception as exc:
            logger.error("plivo media stream failed for %s: %s", call_id, exc)

    answer.__name__ = "plivo_answer"
    hangup.__name__ = "plivo_hangup"
    inbound.__name__ = "plivo_inbound"
    inbound_hangup.__name__ = "plivo_inbound_hangup"
    sms.__name__ = "plivo_sms"
    stream.__name__ = "plivo_stream"

    router.add_api_route("/answer/{call_id}", answer, methods=["POST"], operation_id=answer.__name__)
    router.add_api_route("/hangup/{call_id}", hangup, methods=["POST"], operation_id=hangup.__name__)
    router.add_api_route("/inbound", inbound, methods=["POST"], operation_id=inbound.__name__)
    router.add_api_route("/inbound_hangup", inbound_hangup, methods=["POST"], operation_id=inbound_hangup.__name__)
    router.add_api_route("/sms", sms, methods=["POST"], operation_id=sms.__name__)
    router.add_api_websocket_route("/stream/{call_id}", stream, name=stream.__name__)
    return router


def all_webhook_routers() -> list[APIRouter]:
    routers = [build_laml_router(name) for name in ("twilio", "telnyx")]
    routers.append(build_plivo_router())
    return routers
