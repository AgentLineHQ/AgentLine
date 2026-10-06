# Changelog

Notable changes to the open-source AgentLine tree, newest first.
Version numbers match `agentline/main.py`.

## [0.3.0] — 2026-10-06

Self-hosters bring their own carrier, speech vendors, and voice stack.
The default path is unchanged: SignalWire, Deepgram, an OpenAI-compatible LLM, and Cartesia.

### Added

- Telephony providers. `TELEPHONY_PROVIDER` selects `signalwire` (default), `twilio`, `plivo`, `telnyx`, or `module:Class`. `register_telephony()` registers a custom carrier. Each number and call stores the provider that owns it, and webhook routes for all four built-in carriers stay mounted.
- Voice runtimes. `VOICE_RUNTIME` selects `builtin` (default), `livekit`, `pipecat`, or `module:Class`. An agent can override that with `voice_runtime`. LiveKit can dial a SIP URI or bridge media in-process. Pipecat runs an optional bot factory (`PIPECAT_FACTORY` or `register_pipecat_factory()`).
- Speech and model hooks on the built-in pipeline. `STT_PROVIDER`, `TTS_PROVIDER`, and `LLM_PROVIDER` default to Deepgram, Cartesia, and an OpenAI-compatible API. `register_stt()`, `register_tts()`, and `register_llm()` swap them in process.
- Shared call lifecycle in `agentline/call_flow.py` for answer XML, media, hangup, and inbound SMS.
- Migration `migrations/007_open_source_hooks.sql`, plus the same column changes on startup. Adds `phone_numbers.provider`, `calls.provider`, and `agents.voice_runtime`.
- Optional installs: `requirements-livekit.txt` and `requirements-pipecat.txt`. Core `requirements.txt` does not pull those stacks in.
- Hook guide in `docs/providers.md`, sample registrations in `examples/hooks.py`, and `tests/test_hooks.py`.
- API keys. Bearer auth accepts `al_live_` (current prefix) and `sk_live_` (legacy prefix).

### Removed

- Hosted billing. Balance checks, the billing ledger, usage and billing routes, and per-call debits are gone. You pay your own carrier and speech vendors.
- Supabase. `accounts.supabase_user_id` and the Supabase email client are gone. Accounts are API keys stored in Postgres.
- The unused Telnyx SDK client and the Plivo SDK dependency. Twilio, Plivo, and Telnyx talk to their HTTP APIs through `httpx`.

### Breaking

- Startup and migration 007 drop `accounts.balance`, `accounts.supabase_user_id`, and `billing_ledger` on an existing database. That data is not migrated. Take a backup before upgrading a database that still has hosted billing rows.
- `/billing` and `/usage` routes are gone. Clients that called them need to stop.
- Buying or attaching a number no longer checks a balance, and the route no longer forces a US `+1` number. SignalWire's own client still provisions US numbers only. Other carriers apply their own rules.

## [0.2.0] — 2026-06-22

Initial open-source release (`9ba3610`).

- FastAPI REST API for agents, phone numbers, calls, SMS, events, and voice settings.
- Built-in voice pipeline: Deepgram speech-to-text, Cartesia text-to-speech, OpenAI LLM.
- SignalWire for numbers, calls, and SMS, with an unused Telnyx client module in tree.
- MCP server and a one-file agent skill.
- Event mailbox for agents that poll instead of holding a websocket.
- Hosted billing (balance and a ledger) and a Supabase user link. Both were removed in 0.3.0.
- Docker Compose for the API, Postgres, and Redis. MIT license.

Documentation-only commits on this release, with no version bump:

- 2026-06-22 — Removed the Railway one-click deploy button (`4efc540`).
- 2026-06-22 — Skill file: MCP usage, balance check, a bash fix, voicemail detection, and a longer billing section (`babc2a8`). The billing parts of that skill no longer apply after 0.3.0.
- 2026-07-05 — Updated the Discord invite link (`9a687bc`).
