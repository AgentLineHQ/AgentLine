"""
AgentLine — FastAPI Application Entry Point
Mounts all routers and manages startup/shutdown lifecycle.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentline.database import init_db, close_db
from agentline.redis_client import init_redis, close_redis
from agentline.routers import agents, numbers, messages, calls, events, voice_settings
from agentline.routers.webhooks import all_webhook_routers, webhook_operation_ids

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agentline")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of database and Redis connections."""
    logger.info("Starting AgentLine...")
    await init_db()
    await init_redis()

    try:
        await _reconfigure_number_callbacks()
    except Exception as e:
        logger.warning("Non-fatal: failed to reconfigure number callbacks on startup: %s", e)

    logger.info("AgentLine ready.")
    yield
    logger.info("Shutting down AgentLine...")
    await close_redis()
    await close_db()
    logger.info("AgentLine stopped.")


async def _reconfigure_number_callbacks():
    """Point active numbers at this server's webhook URLs for their carrier."""
    from agentline.providers.registry import get_telephony

    provider = get_telephony()
    if not provider.is_configured():
        logger.info("Skipping number webhook refresh — %s is not configured.", provider.name)
        return
    await provider.reconfigure_active_numbers()


app = FastAPI(
    title="AgentLine — Phone Number for AI Agents",
    description=(
        "AI-native telephony platform that gives your AI agent a real phone number, "
        "a human-like voice, and the ability to make and receive phone calls autonomously. "
        "Build AI phone agents, automated outbound calling systems, AI receptionists, "
        "and conversational voice AI assistants over real phone lines."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers

app.include_router(agents.router)
app.include_router(numbers.router)
app.include_router(messages.router)
app.include_router(calls.router)
app.include_router(events.router)
app.include_router(voice_settings.router)
for _webhook_router in all_webhook_routers():
    app.include_router(_webhook_router)


@app.get("/", tags=["Health"], operation_id="health_check")
async def root():
    return {
        "service": "AgentLine",
        "version": "0.3.0",
        "status": "operational",
        "mcp_endpoint": "/mcp",
    }


@app.get("/health", tags=["Health"], operation_id="health_status")
async def health():
    status = "healthy"
    try:
        from agentline.database import get_db_conn
        async with get_db_conn() as db:
            await db.fetchval("SELECT 1")
    except Exception as e:
        status = f"unhealthy: {e}"
    
    return {"status": status}


@app.get("/debug/urls", tags=["Health"], operation_id="debug_callback_urls")
async def debug_urls():
    """Show the callback URLs carriers should call, and which hooks are active."""
    from agentline.config import settings
    from agentline.providers.base import public_http_url, public_ws_url
    from agentline.providers.registry import get_telephony, registered_telephony
    from agentline.voice.hooks import registered_llm, registered_stt, registered_tts
    from agentline.voice.runtime import registered_runtimes

    provider = get_telephony()
    base = settings.base_url_clean
    name = provider.name
    return {
        "base_url_raw": settings.BASE_URL,
        "base_url_clean": base,
        "telephony": {
            "active": name,
            "configured": provider.is_configured(),
            "available": registered_telephony(),
            "answer_url": public_http_url(f"/{name}/answer/call_TEST"),
            "stream_ws_url": public_ws_url(f"/{name}/stream/call_TEST"),
            "hangup_url": public_http_url(f"/{name}/hangup/call_TEST"),
            "inbound_url": public_http_url(f"/{name}/inbound"),
            "inbound_hangup_url": public_http_url(f"/{name}/inbound_hangup"),
            "sms_url": public_http_url(f"/{name}/sms"),
        },
        "voice": {
            "runtime": settings.VOICE_RUNTIME,
            "available_runtimes": registered_runtimes(),
            "stt": settings.STT_PROVIDER,
            "available_stt": registered_stt(),
            "tts": settings.TTS_PROVIDER,
            "available_tts": registered_tts(),
            "llm": settings.LLM_PROVIDER,
            "available_llm": registered_llm(),
        },
    }


# ── MCP Server Integration ────────────────────────────────────
# Exposes all user-facing REST endpoints as MCP tools.
# Internal SignalWire webhooks, debug endpoints, and health checks are excluded.
# Access via: http://localhost:8000/mcp (or your deployed URL + /mcp)

from fastapi_mcp import FastApiMCP
from mcp import types as mcp_types

mcp = FastApiMCP(
    app,
    name="AgentLine",
    description=(
        "AgentLine — Phone number for AI agents | Telephony for AI agents. "
        "A complete AI-native telephony platform that gives your AI agent "
        "a real phone number, a human-like voice, and the ability to make "
        "and receive phone calls autonomously. "
        "Capabilities: buy and manage US phone numbers, create and configure "
        "voice AI agents with custom system prompts, initiate outbound voice "
        "calls, handle inbound calls automatically, retrieve call transcripts, "
        "choose a telephony provider and a voice runtime (built-in, LiveKit, "
        "or Pipecat), set voice preferences, and poll for "
        "real-time call events. "
        "Use cases: AI phone agents, automated outbound calling, AI receptionist, "
        "voice AI assistants, phone-based customer support bots, "
        "conversational AI over the phone, and programmable telephony for LLMs. "
        "Requires Authorization: Bearer al_live_xxx header (legacy sk_live_ keys also accepted)."
    ),
    describe_full_response_schema=True,
    describe_all_responses=True,
    # Exclude internal webhooks, health/debug endpoints, and tools
    # not documented in the public skill (SKILL.md).
    exclude_operations=[
        # Carrier webhooks are not agent tools.
        *webhook_operation_ids(),
        # Health / debug
        "health_check",
        "health_status",
        "debug_callback_urls",
        # SMS sending stays off the public tool list.
        "send_sms",
        "list_conversations",
        # Relay-mode call tools.
        "speak_on_call",
        "listen_to_call",
        # Admin / internal tools
        "attach_existing_number",
        "reassign_number",
        "get_phone_number",
    ],
)

# ── Patch MCP Server Metadata ──────────────────────────────────
# FastApiMCP only sets name + description on the underlying Server.
# We patch in version, instructions, and website_url for full metadata.
try:
    mcp.server.version = "0.3.0"
    mcp.server.instructions = (
        "AgentLine gives AI agents real phone numbers and human-like voices. "
        "Start by creating an agent (create_agent), then buy a phone number "
        "(buy_phone_number) to attach to it. The agent can then make outbound "
        "calls (make_outbound_call) and receive inbound calls automatically. "
        "Poll for events (poll_events) to get call completion notifications "
        "and transcripts. Telephony provider and voice runtime are chosen "
        "by the server operator."
    )
    mcp.server.website_url = "https://agentline.ai"
except Exception as e:
    logger.warning("Non-fatal: could not patch MCP server metadata: %s", e)

# ── Add Tool Annotations ──────────────────────────────────────
# MCP tool annotations tell clients whether tools are read-only,
# destructive, idempotent, etc. — improving quality scores.
_TOOL_ANNOTATIONS = {
    # Agents
    "create_agent": mcp_types.ToolAnnotations(
        title="Create AI Voice Agent",
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
    ),
    "list_agents": mcp_types.ToolAnnotations(
        title="List AI Voice Agents",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "get_agent": mcp_types.ToolAnnotations(
        title="Get AI Voice Agent Details",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "update_agent": mcp_types.ToolAnnotations(
        title="Update AI Voice Agent",
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "delete_agent": mcp_types.ToolAnnotations(
        title="Delete AI Voice Agent",
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
    ),
    # Numbers
    "buy_phone_number": mcp_types.ToolAnnotations(
        title="Buy Phone Number for AI Agent",
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
    ),
    "list_phone_numbers": mcp_types.ToolAnnotations(
        title="List Phone Numbers",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    # Calls
    "make_outbound_call": mcp_types.ToolAnnotations(
        title="Make Outbound Phone Call",
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
    ),
    "list_calls": mcp_types.ToolAnnotations(
        title="List Voice Calls",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "get_call_details": mcp_types.ToolAnnotations(
        title="Get Call Details",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "get_call_transcript": mcp_types.ToolAnnotations(
        title="Get Call Transcript",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "hangup_call": mcp_types.ToolAnnotations(
        title="Hang Up Phone Call",
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True,
    ),
    # Messages
    "list_messages": mcp_types.ToolAnnotations(
        title="List SMS Messages",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    # Events
    "poll_events": mcp_types.ToolAnnotations(
        title="Poll Telephony Events",
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
    ),
    "peek_events": mcp_types.ToolAnnotations(
        title="Peek at Pending Events",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    # Voice
    "list_available_voices": mcp_types.ToolAnnotations(
        title="List Available Voices",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "get_account_voice": mcp_types.ToolAnnotations(
        title="Get Account Voice Setting",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "set_account_voice": mcp_types.ToolAnnotations(
        title="Set Account Voice",
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "reset_account_voice": mcp_types.ToolAnnotations(
        title="Reset Account Voice to Default",
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
}

# Apply annotations to each tool
for tool in mcp.tools:
    if tool.name in _TOOL_ANNOTATIONS:
        tool.annotations = _TOOL_ANNOTATIONS[tool.name]

mcp.mount_http(mount_path="/mcp")

logger.info("MCP server mounted at /mcp with %d tools", len(mcp.tools))

