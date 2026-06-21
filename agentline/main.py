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
from agentline.routers import agents, numbers, messages, calls, usage, events, signalwire_events, billing_api, voice_settings

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

    # Reconfigure existing numbers with correct StatusCallback for billing
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
    """
    Ensure all active SignalWire numbers have the correct StatusCallback URL
    so inbound call hangups are properly received and billed.
    Runs once on startup — safe to call repeatedly (idempotent).
    """
    import httpx
    from agentline.config import settings
    from agentline.database import get_db_conn

    if not all([settings.SIGNALWIRE_PROJECT_ID, settings.SIGNALWIRE_TOKEN, settings.SIGNALWIRE_SPACE_URL]):
        logger.info("Skipping number callback reconfiguration — SignalWire not configured.")
        return

    sw_base = f"https://{settings.SIGNALWIRE_SPACE_URL}/api/laml/2010-04-01/Accounts/{settings.SIGNALWIRE_PROJECT_ID}"
    auth = (settings.SIGNALWIRE_PROJECT_ID, settings.SIGNALWIRE_TOKEN)
    base = settings.base_url_clean

    async with get_db_conn() as db:
        rows = await db.fetch(
            "SELECT id, phone_number, provider_id FROM phone_numbers WHERE status = 'active'"
        )

    if not rows:
        return

    logger.info("Reconfiguring StatusCallback on %d active number(s)...", len(rows))
    async with httpx.AsyncClient(timeout=10.0) as client:
        for r in rows:
            try:
                await client.post(
                    f"{sw_base}/IncomingPhoneNumbers/{r['provider_id']}.json",
                    auth=auth,
                    data={
                        "VoiceUrl": f"{base}/signalwire/inbound",
                        "VoiceMethod": "POST",
                        "SmsUrl": f"{base}/signalwire/sms",
                        "SmsMethod": "POST",
                        "StatusCallback": f"{base}/signalwire/inbound_hangup",
                        "StatusCallbackMethod": "POST",
                    },
                )
                logger.info("  ✓ %s — StatusCallback updated", r["phone_number"])
            except Exception as e:
                logger.warning("  ✗ %s — failed: %s", r["phone_number"], e)


app = FastAPI(
    title="AgentLine — Phone Number for AI Agents",
    description=(
        "AI-native telephony platform that gives your AI agent a real phone number, "
        "a human-like voice, and the ability to make and receive phone calls autonomously. "
        "Build AI phone agents, automated outbound calling systems, AI receptionists, "
        "and conversational voice AI assistants over real phone lines."
    ),
    version="0.2.0",
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
app.include_router(usage.router)
app.include_router(events.router)
app.include_router(signalwire_events.router)
app.include_router(billing_api.router)
app.include_router(voice_settings.router)


@app.get("/", tags=["Health"], operation_id="health_check")
async def root():
    return {
        "service": "AgentLine",
        "version": "0.2.0",
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
    """Show the callback URLs that providers will receive — useful for debugging."""
    from agentline.config import settings
    base = settings.base_url_clean
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return {
        "base_url_raw": settings.BASE_URL,
        "base_url_clean": base,
        "signalwire": {
            "answer_url": f"{base}/signalwire/answer/call_TEST",
            "stream_ws_url": f"{ws_base}/signalwire/stream/call_TEST",
            "hangup_url": f"{base}/signalwire/hangup/call_TEST",
            "inbound_url": f"{base}/signalwire/inbound",
            "inbound_hangup_url": f"{base}/signalwire/inbound_hangup",
            "sms_url": f"{base}/signalwire/sms",
        },
        "voice_pipeline": {
            "stt": "Deepgram Nova-2 ($0.006/min)",
            "tts": "Cartesia Sonic ($0.002/min)",
            "llm": "GPT-4o-mini / GPT-4o",
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
        "manage billing and usage, set voice preferences (TTS), and poll for "
        "real-time call events. "
        "Use cases: AI phone agents, automated outbound calling, AI receptionist, "
        "voice AI assistants, phone-based customer support bots, "
        "conversational AI over the phone, and programmable telephony for LLMs. "
        "Requires Authorization: Bearer sk_live_xxx header."
    ),
    describe_full_response_schema=True,
    describe_all_responses=True,
    # Exclude internal webhooks, health/debug endpoints, and tools
    # not documented in the public skill (SKILL.md).
    exclude_operations=[
        # ── Internal provider webhooks ──
        "signalwire_answer",
        "signalwire_stream",
        "signalwire_hangup",
        "signalwire_inbound_call",
        "signalwire_inbound_hangup",
        "signalwire_sms_callback",
        # ── Health / debug ──
        "health_check",
        "health_status",
        "debug_callback_urls",
        # ── SMS: sending is not enabled ──
        "send_sms",
        "list_conversations",
        # ── Relay-mode call tools (hosted mode only) ──
        "speak_on_call",
        "listen_to_call",
        # ── Billing: only balance + expenditure exposed ──
        "get_usage_stats",
        "get_usage_balance",
        "get_billing_transactions",
        "get_spending_summary",
        "get_call_charges",
        "get_number_charges",
        "verify_call_billing",
        # ── Admin / internal tools ──
        "topup_balance",
        "attach_existing_number",
        "reassign_number",
        "get_phone_number",
    ],
)

# ── Patch MCP Server Metadata ──────────────────────────────────
# FastApiMCP only sets name + description on the underlying Server.
# We patch in version, instructions, and website_url for full metadata.
try:
    mcp.server.version = "0.2.0"
    mcp.server.instructions = (
        "AgentLine gives AI agents real phone numbers and human-like voices. "
        "Start by creating an agent (create_agent), then buy a phone number "
        "(buy_phone_number) to attach to it. The agent can then make outbound "
        "calls (make_outbound_call) and receive inbound calls automatically. "
        "Poll for events (poll_events) to get call completion notifications "
        "and transcripts. Check your balance (get_account_balance) before "
        "making calls or buying numbers."
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
    # Billing
    "get_account_balance": mcp_types.ToolAnnotations(
        title="Get Account Balance",
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
    "get_expenditure_breakdown": mcp_types.ToolAnnotations(
        title="Get Expenditure Breakdown",
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

