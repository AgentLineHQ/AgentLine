<div align="center">
  <img src="agentline-logo-200.png" alt="AgentLine Logo" width="120" />
  <h1>AgentLine</h1>
  <p><strong>Open-source telephony API for AI agents — phone numbers, voice calls, and SMS</strong></p>
  <p>Give your AI agent a real phone number. Make outbound calls, receive inbound calls, and handle SMS — all through one API. No telecom expertise needed.</p>

  <br/>

  [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
  [![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776AB.svg?logo=python&logoColor=white)](https://python.org)
  [![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
  [![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-8B5CF6.svg)](https://modelcontextprotocol.io)
  [![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ED.svg?logo=docker&logoColor=white)](Dockerfile)
  [![Discord](https://img.shields.io/badge/Discord-Join-5865F2.svg?logo=discord&logoColor=white)](https://discord.gg/69SVE2jWNr)

  <br/>

  [Website](https://agentline.cloud) · [Docs](https://agentline.cloud/docs) · [Changelog](CHANGELOG.md) · [Skill File](https://agentline.cloud/skill.md) · [Discord](https://discord.gg/69SVE2jWNr)

  <br/>
</div>

---

## What is AgentLine?

AgentLine is an **open-source AI-native telephony platform** that gives AI agents real phone numbers and human-like voices. It provides a simple REST API and MCP server so AI agents (Claude, Cursor, custom LLM agents) can make and receive phone calls, handle SMS, and retrieve transcripts — without any telecom knowledge.

```
Your AI Agent  →  AgentLine API  →  Real Phone Calls
                                 →  SMS Messages
                                 →  Call Transcripts
```

### Why AgentLine?

| | AgentLine | Twilio/Vonage | Build It Yourself |
|---|---|---|---|
| **Built for AI agents** | ✅ Purpose-built | ❌ Built for call centers | ❌ You stitch it together |
| **Setup time** | 5 minutes | Hours | Weeks |
| **MCP server** | ✅ Native | ❌ None | ❌ Build your own |
| **Skill file install** | ✅ One file | ❌ Complex SDK | ❌ Hundreds of lines |
| **Voice pipeline** | ✅ Included (STT + TTS + LLM) | ❌ BYO | ❌ BYO |
| **Open source** | ✅ MIT | ❌ Proprietary | ✅ Your code |

---

## Features

- 📞 **Voice Calls** — Make and receive real phone calls through a simple API
- 🎙️ **AI Voice Pipeline** — Built-in STT (Deepgram) + LLM (GPT-4o) + TTS (Cartesia) pipeline
- 💬 **SMS** — Receive and read inbound text messages
- 🔌 **MCP Server** — Native Model Context Protocol support for Claude Desktop and Cursor
- 📋 **Skill File** — One-file install for any AI agent (Claude Code, Cursor, OpenClaw)
- 🌍 **Pluggable carriers** — SignalWire, Twilio, Plivo, Telnyx, or your own class
- 🎙️ **Pluggable voice runtimes** — built-in pipeline, [LiveKit](https://livekit.io), [Pipecat](https://github.com/pipecat-ai/pipecat), or your own
- 📝 **Transcripts** — Automatic call transcription with full conversation history
- 🔄 **Persistent Agent Relay** — outbound WebSocket with reconnect, ACK/replay, turn-safe context, and runtime-aware setup
- 📬 **Event Mailbox** — durable fallback for agents without a relay or webhook
- 🐳 **Docker Ready** — One command to run the API, Postgres, and Redis locally

---

## Architecture

```mermaid
graph LR
    A["🤖 AI Agent"] -->|REST API / MCP| B["⚡ AgentLine API"]
    B -->|Telephony hook| C["📱 SignalWire, Twilio, Plivo, Telnyx, or yours"]
    B -->|Voice runtime hook| H["🎙️ Built-in, LiveKit, or Pipecat"]
    H -->|Built-in only| D["🎤 STT hook"]
    H -->|Built-in only| E["🔊 TTS hook"]
    H -->|Built-in only| F["🧠 LLM hook"]
    C -->|Voice and SMS| G["📞 PSTN"]
    B -->|Events and transcripts| A
```

### Voice runtimes

`VOICE_RUNTIME` picks who holds the conversation:

| Runtime | What it does |
| --- | --- |
| `builtin` | This process runs the STT, LLM, and TTS hooks on the carrier media websocket |
| `livekit` | Creates a LiveKit room, dispatches your agent, and either dials LiveKit SIP or bridges the carrier audio |
| `pipecat` | Runs a Pipecat bot on the carrier websocket. Bring your own with `PIPECAT_FACTORY` |
| `module:Class` | Your runtime. Implement `prepare` and `run` |

The built-in hooks default to Deepgram, any OpenAI-compatible chat API, and Cartesia. Swap each one with `STT_PROVIDER`, `LLM_PROVIDER`, and `TTS_PROVIDER`. An agent can override the runtime with `voice_runtime`.

Carriers, runtimes, and the exact method signatures are in [docs/providers.md](docs/providers.md). A copy-paste registration example is in [examples/hooks.py](examples/hooks.py).

---

## Quick Start

### Option 1: Docker Compose (Recommended)

```bash
git clone https://github.com/agentlineHQ/AgentLine.git
cd AgentLine
cp .env.example .env
# Fill in your API keys in .env (see Configuration below)
docker-compose up -d
```

This starts:
- **API server** at `http://localhost:8000`
- **PostgreSQL** at `localhost:5432` (schema auto-applied)
- **Redis** at `localhost:6379`

### Option 2: Local Development

```bash
git clone https://github.com/agentlineHQ/AgentLine.git
cd AgentLine
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys
uvicorn agentline.main:app --reload
```

### Option 3: Use the Hosted Version

Skip self-hosting — sign up at [agentline.cloud](https://agentline.cloud) and get an API key instantly.

---

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string. Docker Compose fills this in |
| `TELEPHONY_PROVIDER` | No | `signalwire` (default), `twilio`, `plivo`, `telnyx`, or `module:Class` |
| `VOICE_RUNTIME` | No | `builtin` (default), `livekit`, `pipecat`, or `module:Class` |
| `SIGNALWIRE_PROJECT_ID` | For SignalWire | SignalWire project ID |
| `SIGNALWIRE_TOKEN` | For SignalWire | SignalWire API token |
| `SIGNALWIRE_SPACE_URL` | For SignalWire | Space hostname, such as `example.signalwire.com` |
| `TWILIO_ACCOUNT_SID` | For Twilio | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | For Twilio | Twilio auth token |
| `PLIVO_AUTH_ID` | For Plivo | Plivo auth id |
| `PLIVO_AUTH_TOKEN` | For Plivo | Plivo auth token |
| `TELNYX_API_KEY` | For Telnyx | Telnyx API key |
| `TELNYX_ACCOUNT_SID` | For Telnyx | TeXML application id |
| `STT_PROVIDER` | No | `deepgram` or `module:Class` |
| `TTS_PROVIDER` | No | `cartesia` or `module:Class` |
| `LLM_PROVIDER` | No | `openai` or `module:Class` |
| `DEEPGRAM_API_KEY` | For the default STT | Deepgram API key |
| `CARTESIA_API_KEY` | For the default TTS | Cartesia API key |
| `OPENAI_API_KEY` | For the default LLM | OpenAI-compatible API key |
| `OPENAI_BASE_URL` | No | Defaults to `https://api.openai.com/v1` |
| `LIVEKIT_URL` | For LiveKit | LiveKit server URL |
| `LIVEKIT_API_KEY` | For LiveKit | LiveKit API key |
| `LIVEKIT_API_SECRET` | For LiveKit | LiveKit API secret |
| `LIVEKIT_SIP_URI` | No | When set, the carrier dials LiveKit SIP. Supports `{room}` and `{call_id}` |
| `PIPECAT_FACTORY` | No | `module:function` that replaces the default Pipecat bot |
| `REDIS_URL` | No | Redis URL (defaults to `localhost:6379`) |
| `SECRET_KEY` | Yes | App secret key |
| `BASE_URL` | Yes | Public URL of your deployment |

LiveKit's in-process audio bridge needs `pip install -r requirements-livekit.txt`. The default Pipecat bot needs `pip install -r requirements-pipecat.txt`. SIP mode for LiveKit does not need the LiveKit Python package.

---

## API Reference

Once running, visit `http://localhost:8000/docs` for the interactive Swagger UI.

### Core Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/agents` | Create a new AI voice agent |
| `GET` | `/v1/agents` | List all agents |
| `PATCH` | `/v1/agents/{id}` | Update agent (prompt, voice, greeting) |
| `POST` | `/v1/numbers` | Provision a phone number |
| `GET` | `/v1/numbers` | List phone numbers |
| `POST` | `/v1/calls` | Make an outbound call |
| `GET` | `/v1/calls` | List calls |
| `GET` | `/v1/calls/{id}/transcript` | Get call transcript |
| `POST` | `/v1/calls/{id}/hangup` | End an active call |
| `GET` | `/.well-known/agentline.json` | Discover relay, webhook, and runtime setup |
| `GET` | `/v1/events` | Poll event mailbox (fallback) |
| `WS` | `/v1/events/ws` | Persistent outbound agent relay |
| `GET` | `/v1/events/peek` | Peek at events (non-destructive) |
| `GET` | `/v1/messages` | List SMS messages |
| `GET` | `/debug/urls` | Active carrier, voice runtime, and callback URLs |

### Authentication

All requests require an API key:

```bash
curl -H "Authorization: Bearer sk_live_YOUR_KEY" \
     -H "Content-Type: application/json" \
     http://localhost:8000/v1/agents
```

---

## MCP Server Integration

AgentLine includes a built-in **MCP (Model Context Protocol) server**, so AI agents like Claude Desktop and Cursor can use telephony tools natively.

### Connect from Claude Desktop

Add to your Claude Desktop config:

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "agentline": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote@latest",
        "http://localhost:8000/mcp",
        "--header", "Authorization: Bearer sk_live_YOUR_KEY"
      ]
    }
  }
}
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `create_agent` | Create a new AI voice agent |
| `list_agents` | List all agents |
| `update_agent` | Update agent config/prompt/voice |
| `make_outbound_call` | Initiate an outbound phone call |
| `list_calls` | List call history |
| `get_call_transcript` | Get the full transcript of a call |
| `hangup_call` | End an active call |
| `buy_phone_number` | Provision a new phone number |
| `list_phone_numbers` | List all phone numbers |
| `poll_events` | Poll event mailbox |
| `peek_events` | Peek at pending events |
| `list_available_voices` | List voice presets |

### Test with MCP Inspector

```bash
npx @modelcontextprotocol/inspector http://localhost:8000/mcp
```

---

## Skill File — Install in 30 Seconds

AgentLine ships with a **skill file** that lets any AI agent gain telephony powers instantly:

```
https://agentline.cloud/skill.md
```

**How to use:**
1. Copy the skill URL above
2. Add it to your AI agent (Claude Code, Cursor, OpenClaw, etc.)
3. Set your `AGENTLINE_API_KEY` environment variable
4. Tell your agent: *"Call +1234567890"* — it just works!

The skill file is also included in this repo at [`skills/agentline/SKILL.md`](skills/agentline/SKILL.md).

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **API Framework** | [FastAPI](https://fastapi.tiangolo.com) (async Python) |
| **Database** | PostgreSQL |
| **Cache** | Redis |
| **Phone numbers and calls** | SignalWire, Twilio, Plivo, Telnyx, or your class |
| **Voice runtime** | Built-in pipeline, LiveKit, or Pipecat |
| **Default speech-to-text** | Deepgram Nova-2, replaceable |
| **Default text-to-speech** | Cartesia Sonic, replaceable |
| **Default LLM** | Any OpenAI-compatible API |
| **MCP Server** | [FastAPI-MCP](https://github.com/tadata-org/fastapi-mcp) |
| **Deployment** | Docker, [Railway](https://railway.app) |

---

## Deployment

### Docker

```bash
docker build -t agentline .
docker run -p 8000:8000 --env-file .env agentline
```

### Any Cloud Provider

AgentLine runs anywhere that supports Python 3.12+ and Docker: AWS, GCP, Azure, Fly.io, Render, Railway, etc.

---

## Database Schema

The database schema is in [`schema.sql`](schema.sql). It creates tables for:

- **accounts** — API accounts
- **api_keys** — Hashed API keys for authentication
- **agents** — AI voice agent configurations, including an optional `voice_runtime`
- **phone_numbers** — Provisioned phone numbers and which carrier owns them
- **calls** — Call records with transcripts
- **messages** — SMS message records
- **event_mailbox** — Server-side event queue

Migrations are in the [`migrations/`](migrations/) directory.

---

## Cost

This repository does not bill anyone. You pay the carrier, speech vendor, model host, LiveKit, or Pipecat deployment you configure.

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

- 🐛 **Bug reports** — [Open an issue](https://github.com/agentlineHQ/AgentLine/issues/new?template=bug_report.md)
- 💡 **Feature requests** — [Open an issue](https://github.com/agentlineHQ/AgentLine/issues/new?template=feature_request.md)
- 🔧 **Pull requests** — Fork, branch, PR

---

## Community

- 💬 [Discord](https://discord.gg/69SVE2jWNr) — Chat with the team and other builders
- 🐦 [Twitter](https://twitter.com/ovalpod94416) — Updates and announcements
- 📖 [Docs](https://agentline.cloud/docs) — Full API documentation
- 📝 [Blog](https://agentline.cloud/blogs) — Guides and tutorials

---

## License

[MIT](LICENSE) — use it for anything. Commercial use welcome.

---

<div align="center">
  <p>Built with ❤️ by the <a href="https://agentline.cloud">AgentLine</a> team</p>
  <p><sub>Give your AI agent a voice. ☎️</sub></p>
</div>
