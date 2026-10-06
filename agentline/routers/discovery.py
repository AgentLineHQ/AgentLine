"""Machine-readable integration discovery for autonomous agent runtimes."""

from fastapi import APIRouter

from agentline.config import settings


router = APIRouter(tags=["Agent Integration"])


@router.get("/.well-known/agentline.json", operation_id="agentline_integration_discovery")
async def agentline_integration_discovery():
    base = settings.base_url_clean
    ws_base = settings.ws_base_url
    return {
        "protocol": "agentline-relay/1",
        "api_base": base,
        "purpose": "Receive phone events and answer live callers through turn-correlated context.",
        "authentication": {
            "type": "bearer",
            "header": "Authorization: Bearer <AGENTLINE_API_KEY>",
        },
        "recommended_transport": "websocket",
        "easy_setup": {
            "connector_url": f"{base}/static/agentline_relay.py",
            "steps": [
                "Download connector_url to agentline_relay.py.",
                "Run: python agentline_relay.py install --agent-id <agent_id>",
            ],
            "behavior": (
                "Auto-detects Hermes, OpenClaw, Claude Code, or Codex; "
                "preserves sessions; installs a persistent user service; "
                "stores non-live events in a private local inbox."
            ),
            "required_environment": ["AGENTLINE_API_KEY"],
        },
        "transports": {
            "websocket": {
                "url_template": f"{ws_base}/v1/events/ws?agent_id={{agent_id}}&runtime={{runtime}}",
                "direction": "agent connects outbound to AgentLine",
                "supports": ["ack", "replay", "live_context", "heartbeat"],
            },
            "webhook": {
                "configure": f"{base}/v1/webhooks",
                "use_when": "The runtime can expose a stable public HTTPS route.",
            },
            "polling": {
                "url": f"{base}/v1/events",
                "use_when": "Completion/SMS events only; not recommended for live call turns.",
            },
        },
        "runtime_selection": {
            "hermes": "Bundled connector: local Responses API, named conversation, and X-Hermes-Session-Key per call.",
            "openclaw": "Bundled connector: Gateway-backed openclaw agent --session-key per call.",
            "claude-code": "Bundled connector: capture the CLI session_id and resume that exact session on later turns.",
            "codex": "Bundled connector: capture the codex exec thread ID and resume that exact thread on later turns.",
            "unknown": "Use the generic outbound WebSocket. No runtime-specific integration is required.",
        },
        "context_response": {
            "correlation": "turn_id",
            "http_url_template": f"{base}/v1/calls/{{call_id}}/context?turn_id={{turn_id}}",
            "websocket_frame": {
                "type": "context",
                "call_id": "call_...",
                "turn_id": "turn_...",
                "push_token": "token from call.utterance",
                "context": "caller-ready response spoken verbatim",
            },
            "response_mode": "direct_speech",
            "rule": "Return concise caller-ready speech. Always echo call_id, turn_id, and push_token. Never apply a result to a different turn.",
        },
    }
