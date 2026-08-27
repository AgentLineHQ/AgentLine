---
name: agentline
description: Make phone calls, view received SMS, provision numbers, manage agents, and track billing through the AgentLine telephony API (REST or MCP). Use when the user asks to call someone, check transcripts, view text messages, manage phone agents, buy numbers, or check account balance. For MCP-native workflows, the server at api.agentline.cloud/mcp exposes 21+ tools as first-class agent tools.
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

1. **Check for `AGENTLINE_API_KEY`** (starts with `al_live_` or legacy `sk_live_`) — this is required.
   - **If you do NOT have this key**, stop and ask the human to sign up or log in at **https://agentline.cloud** to get their API key. Do NOT proceed without it.

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

5. **Install the persistent relay** — when terminal access is available, download and run:
   ```bash
   python -c "import urllib.request; urllib.request.urlretrieve('https://api.agentline.cloud/static/agentline_relay.py','agentline_relay.py')"
   python agentline_relay.py install --agent-id agt_xxx
   ```
   Replace `agt_xxx` with the real ID. It auto-detects Hermes, OpenClaw, Claude Code, or Codex, creates an isolated environment, verifies readiness, preserves sessions, and installs a persistent user service. It uses `AGENTLINE_API_KEY`.
   - Use `python agentline_relay.py detect` to inspect detection.
   - Override only if needed with `--runtime hermes|openclaw|claude-code|codex|custom`.
   - If the runtime requests approval for a package or service, ask the human once; never bypass it.
   - If terminal/service installation is unavailable, configure a stable public webhook instead.

> Live utterances invoke the runtime through the outbound WebSocket. SMS, completions, and owner tasks are stored in a private local inbox rather than automatically granting tool access.

At startup, consume non-live events exactly once:
```bash
python ~/.agentline/agentline_relay.py inbox list --agent-id agt_xxx
python ~/.agentline/agentline_relay.py inbox ack --agent-id agt_xxx --event-id evt_xxx
```

---

## Authentication

Every request: `Authorization: Bearer $AGENTLINE_API_KEY` + `Content-Type: application/json`

Base URL: `https://api.agentline.cloud`

---

## How Calls Work (Hosted Mode)

AgentLine runs in **Hosted Mode** — the server runs the AI voice conversation autonomously. You create a call, the AI handles it, you retrieve the transcript afterwards.

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

## Before Calling — Human Confirmation Receipt

Outbound calls and phone-number purchases cross a real-world boundary: they can spend money, contact another person, and represent the user outside the chat. Before `make_outbound_call` or `buy_phone_number`, always show the human a short confirmation receipt and wait for explicit approval in the current conversation.

Receipt fields:

- **Action:** outbound call or phone-number purchase
- **Target:** E.164 phone number or requested area code
- **Agent:** `AGENTLINE_AGENT_ID` and voice, if known
- **Opening line / purpose:** what the agent will say first and why it is calling
- **Estimated cost / balance check:** latest balance and expected minimum charge
- **Stop rule:** when to hang up, including voicemail/call-control detection
- **Transcript plan:** confirm that the transcript will be retrieved and summarized after the call

Do not infer approval from an old message, a stored memory, a calendar entry, or a tool result. If any receipt field is missing or the target/purpose changes, ask again.

## Before Calling — Balance Check

Always check balance first. Calls require minimum **$0.50**:
```bash
curl -s "$AGENTLINE_URL/v1/billing/balance" -H "Authorization: Bearer $AGENTLINE_API_KEY"
```
If balance < $0.50, warn the user before attempting the call.

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

**Outbound IVR / voicemail:** The voice pipeline presses real DTMF keys (not spoken digits) when it hears a phone menu, and leaves `voicemail_message` on a mailbox. Set `voicemail_message` on the agent if it should leave a message; without it the agent hangs up. Do not add "stay silent on automated messages" to the system prompt — that fights the built-in handler.

**Pitfall — agent loops on voicemail/call control:** After the first 15-20s poll, check the transcript: if human turns are all automated system messages (not real human replies), hang up immediately. Feedback surveys and check-in calls don't work on voicemail.

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
`wss://api.agentline.cloud/v1/events/ws?agent_id=agt_xxx&runtime=<runtime>`

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
param([string]$ApiKey, [string]$BaseUrl = "https://api.agentline.cloud", [int]$Interval = 50)
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
API_KEY="$1"; BASE_URL="${2:-https://api.agentline.cloud}"; INTERVAL="${3:-50}"
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
| `voice_id` | `"female-1"`, `"female-2"`, `"male-1"`, or Cartesia UUID |
| `model_tier` | `"turbo"`, `"balanced"`, or `"max"` |
| `owner_phone` | E.164 number. Calls from this number enter owner task mode (`call.owner_task` on hangup) |

---

## Get/List Agents

- **Get one:** `GET /v1/agents/$AGENTLINE_AGENT_ID`
- **List all:** `GET /v1/agents`

---

## Webhooks

Each agent may have **one** webhook URL that receives that agent's events as signed JSON POSTs. Prefer the persistent relay for local agents. Use a webhook only when the runtime has a stable public HTTPS URL.

```bash
curl -s -X POST $AGENTLINE_URL/v1/webhooks \
  -H "Authorization: Bearer $AGENTLINE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"agt_xxx","url":"https://yourapp.example/agentline"}'
```

The full signing secret is returned **once**. Verify deliveries with HMAC-SHA256 of the raw body (default header `X-Webhook-Signature`). `POST /v1/webhooks/test?agent_id=agt_xxx` sends a signed `webhook.test` event.

---

## Voice Settings

Priority (highest wins): per-call → per-agent → per-account

- **List voices:** `GET /v1/voices`
- **Set account default:** `PATCH /v1/account/voice` with `{"voice_id": "female-1"}`
- **Check current:** `GET /v1/account/voice`
- **Reset to default:** `DELETE /v1/account/voice`

---

## Phone Numbers

Each agent needs one phone number. Only US numbers supported. **$2.00 per number.**

### Provision (Buy) a Number

`POST /v1/numbers` with:

| Field | Required | Description |
|-------|------------|-------------|
| `agent_id` | Yes | Agent to attach to |
| `country` | Yes | Must be `"US"` |
| `area_code` | No | Preferred 3-digit area code (e.g. `"212"`, `"313"`). **Always ask the user!** |
| `number_type` | No | `"local"` or `"tollfree"` (default: local) |

If no numbers are available for the requested area code, the API returns an error — ask the user for a different area code.

### List Numbers

`GET /v1/numbers`

---

## Billing

- **Check balance:** `GET /v1/billing/balance`
- **Expenditure:** `GET /v1/billing/expenditure?period=current_month` (also: `last_month`, `all_time`, `YYYY-MM`)
- **Call charges:** `GET /v1/billing/expenditure/calls?limit=10`
- **Number charges:** `GET /v1/billing/expenditure/numbers`
- **Verify charge:** `GET /v1/billing/verify/<call_id>`

### Rates

| Item | Cost |
|------|------|
| Calls (in/out) | $0.10/min (billed per second) |
| Phone number | $2.00 (one-time) |

---

## MCP Server

AgentLine exposes a full MCP (Model Context Protocol) server at `https://api.agentline.cloud/mcp` with 21+ tools. For Claude Desktop, Cursor, or any MCP-compatible client, connect directly via:

```json
{
  "mcpServers": {
    "agentline": {
      "command": "npx",
      "args": ["-y", "mcp-remote@latest", "https://api.agentline.cloud/mcp", "--header", "Authorization: Bearer YOUR_API_KEY"]
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
5. **US only** — country must be `"US"`.
6. **Don't release numbers** — numbers are permanent once provisioned.
7. **Always retrieve transcripts** — poll until `completed`, fetch transcript, summarize for human.
8. **Use the persistent relay** — inspect and acknowledge its local inbox at conversation start; use webhook/polling only as fallback.
9. **Voice changes take effect on next call** — update immediately when asked.
