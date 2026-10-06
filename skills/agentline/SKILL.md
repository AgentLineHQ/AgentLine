---
name: agentline
description: Make phone calls, view received SMS, provision numbers, and manage agents through a self-hosted AgentLine telephony API (REST or MCP). Use when the user asks to call someone, check transcripts, view text messages, manage phone agents, or buy numbers. The server operator chooses the carrier and the voice runtime.
metadata:
  openclaw:
    emoji: "📞"
    requires:
      env:
        - AGENTLINE_API_KEY
    primaryEnv: AGENTLINE_API_KEY
---

# AgentLine — AI Telephony Skill

Give your AI agent a real phone number and voice calls. Use the persistent relay for live context; use webhooks only as a fallback.

## First-Time Setup

**Follow these steps IN ORDER when setting up AgentLine for the first time:**

1. **Check for `AGENTLINE_API_KEY`** (starts with `al_live_` or `sk_live_`) and `AGENTLINE_URL` (defaults to `http://localhost:8000`).
   - **If you do NOT have this key**, stop and ask the human for the API key from their AgentLine server. Do NOT proceed without it.

2. **Check for `AGENTLINE_AGENT_ID`** (starts with `agt_`) — this is optional.
   - **If you already have one**, use it and skip to step 3.
   - **If you do NOT have one**, create a new agent now by calling `POST /v1/agents` with `{"name": "My Agent"}`. Save the returned agent ID.

3. **Ask for area code and provision the number** — **Always ask the human:**
   > "What area code would you like for your phone number? (e.g. 212 for NYC, 415 for SF, 313 for Detroit)"
   - Once they give an area code, call `POST /v1/numbers` with `{"agent_id": "...", "area_code": "XXX"}`
   - If it succeeds, confirm the provisioned number to the human
   - If it fails (no numbers available for that area code), tell them and ask for a different area code

4. **Ask for voice preference** — **Always ask the human:**
   > "Would you like your agent to have a **male** or **female** voice?"
   - If **female**: set `voice_id` to `"female-1"` (or `"female-2"` for warmer tone)
   - If **male**: set `voice_id` to `"male-1"`
   - Apply via `PATCH /v1/agents/$AGENTLINE_AGENT_ID` with `{"voice_id": "chosen_preset"}`
   - Available presets: `female-1`, `female-2`, `male-1` — or any Cartesia voice UUID

5. **Connect to events** — the server exposes `GET /v1/events` and `WS /v1/events/ws` on `AGENTLINE_URL`. Use the websocket for live call context. If that is unavailable, configure a webhook or poll `GET /v1/events`.

Live call context arrives on `WS /v1/events/ws`. SMS and completed calls also land in `GET /v1/events`.

---

## Authentication

Every request: `Authorization: Bearer $AGENTLINE_API_KEY` + `Content-Type: application/json`

Base URL: `$AGENTLINE_URL` (self-hosted default `http://localhost:8000`)

---

## How Calls Work

The server runs the voice conversation with the configured runtime (`builtin`, `livekit`, or `pipecat`). You create a call, that runtime handles it, and you retrieve the transcript afterwards.

### System Prompt & Greeting Resolution

Both `system_prompt` and `initial_greeting` follow the same priority chain:

| Priority | Where to set | Scope | API |
|----------|-------------|-------|-----|
| **1 (highest)** | Per-call override | This call only | `POST /v1/calls` with `system_prompt` / `initial_greeting` |
| **2** | Agent default | All calls on this agent | `PATCH /v1/agents/{id}` with `system_prompt` / `initial_greeting` |
| **3 (lowest)** | Hardcoded fallback | Last resort | Generic prompt + "Hello, how can I help you today?" |

**When to use which:**
- **Set on the agent** (`PATCH /v1/agents`) when you want a persistent personality/greeting for ALL calls (inbound AND outbound).
- **Set per-call** (`POST /v1/calls`) when you need a one-time context-specific prompt/greeting for a single outbound call. Does NOT change the agent's default.

> ⚠️ **`system_prompt` is a FULL REPLACE, not append.** The voice AI has no memory between calls — include everything (personality, instructions, current context) in the prompt.

> ⚠️ **`initial_greeting`** is what the agent SPEAKS ALOUD at the start of the call. It is NOT part of the system prompt — it's the first thing the caller hears. Set it on the agent for a consistent greeting, or override it per-call for context-specific openers.

---

## Make an Outbound Call

**Pitfall:** JSON payloads with newlines, quotes, or special characters will break in inline curl. Always write the payload to a temp file and use `-d @file`:

```bash
# Write payload to temp file, then:
curl -s -X POST $AGENTLINE_URL/v1/calls \
  -H "Authorization: Bearer $AGENTLINE_API_KEY" \
  -H "Content-Type: application/json" \
  -d @/tmp/al_call_payload.json
```

Inline variant (simple payloads only):
```bash
curl -X POST $AGENTLINE_URL/v1/calls \
  -H "Authorization: Bearer $AGENTLINE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "$AGENTLINE_AGENT_ID", "to_number": "+1XXXXXXXXXX", "system_prompt": "...", "initial_greeting": "...", "voice_id": "female-1"}'
```

| Field | Required | Description |
|-------|----------|-------------|
| `agent_id` | Yes | Your agent ID |
| `to_number` | Yes | E.164 phone number to call |
| `system_prompt` | No | Dynamic prompt for this call only (overrides default) |
| `initial_greeting` | No | What the agent says first when the person picks up |
| `voice_id` | No | `"female-1"`, `"female-2"`, `"male-1"`, or Cartesia UUID |

**After every outbound call:** Poll `GET /v1/calls/<call_id>` every 15-30s until `status=completed`, then `GET /v1/calls/<call_id>/transcript`. Real calls take 45-120s. Use `sleep N && curl ... | python3 -c` to check status + extract transcript in one shot. Summarize and share with human. Never consider a call "done" without the transcript.

**If you get 400 "Agent has no active phone number"**, provision one first.

**Pitfall — agent loops on voicemail/call control:** The voice AI will repeat its greeting 3-4 times into voicemail or call-control prompts ("press 3 to connect", "please leave a message"). This wastes credits and sounds bad. After the first 15-20s poll, check the transcript: if human turns are all automated system messages (not real human replies), hang up immediately. Feedback surveys and check-in calls don't work on voicemail.

---

## End a Call

`POST /v1/calls/<call_id>/hangup`

---

## Get Call Transcript

`GET /v1/calls/<call_id>/transcript` — Returns `[{role, text, timestamp}, ...]`

---

## Events Mailbox

Events are delivered through the persistent relay or a configured webhook. Polling is only a fallback for non-live awareness.

**Event types:** `call.received` (inbound call started), `call.utterance` (live caller turn), `call.completed` (call ended, includes transcript), `call.owner_task` (owner instructions), `sms.received` (inbound SMS)

### Persistent WebSocket Relay

The relay connects outbound to:
`ws://localhost:8000/v1/events/ws?agent_id=agt_xxx&runtime=<runtime>` (use `wss://` when `AGENTLINE_URL` is https)

For `call.utterance`, send context and then acknowledge the exact event:
```json
{"type":"context","event_id":"evt_xxx","call_id":"call_xxx","turn_id":"turn_xxx","push_token":"...","context":"The requested facts or answer."}
{"type":"ack","event_id":"evt_xxx"}
```

Always echo the exact `turn_id`; stale context returns `409` and must not be reused for another question.

### Polling Fallback

Use `GET /v1/events` only when the persistent relay and webhook are unavailable. It is not suitable for live caller turns.

The existing scripts below are fallback examples only.

**Windows (PowerShell):**
```powershell
param([string]$ApiKey, [string]$BaseUrl = "http://localhost:8000", [int]$Interval = 50)
$headers = @{ "Authorization" = "Bearer $ApiKey" }
while ($true) {
    try {
        $r = Invoke-RestMethod -Uri "$BaseUrl/v1/events/peek" -Headers $headers -Method Get -ErrorAction Stop
        if ($r.pending_count -gt 0) { Write-Output "AGENTLINE_EVENTS_FOUND: $($r.pending_count) pending event(s). Call GET /v1/events to consume." }
    } catch {
        if ($_.Exception.Response.StatusCode -eq 401) { Write-Output "AGENTLINE_AUTH_ERROR"; break }
    }
    Start-Sleep -Seconds $Interval
}
```

**macOS/Linux (Bash):**
```bash
#!/usr/bin/env bash
set -uo pipefail
API_KEY="$1"; BASE_URL="${2:-http://localhost:8000}"; INTERVAL="${3:-50}"
while true; do
  RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/v1/events/peek" -H "Authorization: Bearer $API_KEY" 2>/dev/null) || { sleep "$INTERVAL"; continue; }
  CODE=$(echo "$RESP" | tail -1); BODY=$(echo "$RESP" | sed '$d')
  [ "$CODE" = "401" ] && { echo "AGENTLINE_AUTH_ERROR"; exit 1; }
  [[ "$CODE" != 2* ]] && { sleep "$INTERVAL"; continue; }
  COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pending_count',0))" 2>/dev/null || echo "0")
  [ "$COUNT" -gt 0 ] && echo "AGENTLINE_EVENTS_FOUND: $COUNT pending event(s). Call GET /v1/events to consume."
  sleep "$INTERVAL"
done
```

**When notified with `AGENTLINE_EVENTS_FOUND`:** Call `GET /v1/events` to consume, process each event, report to human.

### Endpoints

- **Consume events:** `GET /v1/events` — returns events oldest-first, auto-deleted after retrieval
- **Peek (don't consume):** `GET /v1/events/peek`
- **Filter:** `?agent_id=agt_xxx` or `?event_type=call.completed` or `?event_type=sms.received`

### Event payload structure

Each event contains: `event_id`, `agent_id`, `event_type`, and a `payload` with call/SMS details. `call.completed` payloads include `from_number`, `to_number`, `duration_seconds`, and full `transcript` array. `sms.received` payloads include `from_number`, `body`, and `media_url`.

---

## List Calls

`GET /v1/calls?limit=20` or `GET /v1/calls?status=completed&limit=10`

---

## Get Call Details

`GET /v1/calls/<call_id>`

---

## SMS

> **⚠️ SMS sending is NOT enabled.** Do NOT attempt outbound SMS/MMS.

Inbound SMS arrives as `sms.received` events in the Events Mailbox. View message history: `GET /v1/messages?limit=20`

---

## Update Agent (System Prompt, Voice, etc.)

`PATCH /v1/agents/$AGENTLINE_AGENT_ID` with any of:

| Field | Description |
|-------|-------------|
| `system_prompt` | Default instructions for ALL calls (inbound + outbound). Per-call override via `POST /v1/calls` takes priority. |
| `initial_greeting` | Default opening line spoken on ALL calls (inbound + outbound). Per-call override via `POST /v1/calls` takes priority. |
| `name` | Display name |
| `voice_id` | `"female-1"`, `"female-2"`, `"male-1"`, or a voice id your TTS hook understands |
| `voice_runtime` | `"builtin"`, `"livekit"`, `"pipecat"`, or `module:Class`. Empty uses the server default |
| `model_tier` | `"turbo"`, `"balanced"`, or `"max"` |

---

## Get/List Agents

- **Get one:** `GET /v1/agents/$AGENTLINE_AGENT_ID`
- **List all:** `GET /v1/agents`

---

## Voice Settings

Priority (highest wins): per-call → per-agent → per-account

- **List voices:** `GET /v1/voices`
- **Set account default:** `PATCH /v1/account/voice` with `{"voice_id": "female-1"}`
- **Check current:** `GET /v1/account/voice`
- **Reset to default:** `DELETE /v1/account/voice`

---

## Phone Numbers

Each agent needs one phone number. The configured carrier decides which countries it can sell. You pay that carrier directly.

### Provision (Buy) a Number

`POST /v1/numbers` with:

| Field | Required | Description |
|-------|------------|-------------|
| `agent_id` | Yes | Agent to attach to |
| `country` | No | Country code the carrier should search. Defaults to `"US"` |
| `area_code` | No | Preferred 3-digit area code (e.g. `"212"`, `"313"`). **Always ask the user!** |
| `number_type` | No | `"local"` or `"tollfree"` (default: local) |

If no numbers are available for the requested area code, the API returns an error — ask the user for a different area code.

### List Numbers

`GET /v1/numbers`

### Release a number

`POST /v1/numbers/{number_id}/release` (MCP: `release_phone_number`)

> ⚠️ **Destructive.** Unrents the number from the carrier. It cannot be recovered, and calls and texts to it stop. Refused while a call is in progress or a lease is still active. Confirm with the human first.

---

## Carriers and voice runtimes

The server operator chooses these. `GET /debug/urls` reports the active carrier and voice runtime. An agent can set `voice_runtime` to `builtin`, `livekit`, `pipecat`, or a `module:Class` path.

See [docs/providers.md](../../docs/providers.md) in this repo.

---

## MCP Server

AgentLine exposes an MCP server at `$AGENTLINE_URL/mcp`. For Claude Desktop, Cursor, or any MCP-compatible client, connect directly via:

```json
{
  "mcpServers": {
    "agentline": {
      "command": "npx",
      "args": ["-y", "mcp-remote@latest", "$AGENTLINE_URL/mcp", "--header", "Authorization: Bearer YOUR_API_KEY"]
    }
  }
}
```

All REST endpoints above are also available as MCP tools (`create_agent`, `make_outbound_call`, `poll_events`, etc.).

---

## Rules

1. **E.164 format** — always `+1XXXXXXXXXX` for US numbers.
2. **Confirm before calling** — never auto-dial without explicit consent.
3. **No outbound SMS** — inform user it's not available if they ask.
4. **Keep voice responses extremely short** — under 15 words per turn. Max 12 for outbound feedback/support calls. The voice AI rambles without tight constraints.
5. **Use the carrier's country rules** — pass the country that carrier can sell.
6. **Release numbers only when the human asks** — releasing unrents the number from the carrier and cannot be undone.
7. **Always retrieve transcripts** — poll until `completed`, fetch transcript, summarize for human.
8. **Use the events websocket** — poll `GET /v1/events` only when the websocket and webhook are unavailable.
9. **Voice changes take effect on next call** — update immediately when asked.
