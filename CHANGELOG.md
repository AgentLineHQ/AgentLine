# Changelog

Notable changes to the open-source AgentLine tree, newest first.
Version numbers match `agentline/main.py`.

## [0.4.0] — 2026-10-06

Self-hosters bring their own carrier and voice stack. The default path is unchanged: SignalWire, Deepgram, an OpenAI-compatible LLM, and Cartesia, including the relay, DTMF, and owner-mode behavior from 0.3.0.

### Added

- Telephony providers. `TELEPHONY_PROVIDER` selects `signalwire` (default), `twilio`, `plivo`, `telnyx`, or `module:Class`. `register_telephony()` registers a custom carrier. Each number and call stores the provider that owns it.
- Voice runtimes. `VOICE_RUNTIME` selects `builtin` (default), `livekit`, `pipecat`, or `module:Class`. An agent can override that with `voice_runtime`. LiveKit can dial a SIP URI or bridge media in-process. Pipecat runs an optional bot factory (`PIPECAT_FACTORY` or `register_pipecat_factory()`). On SignalWire, a non-builtin runtime replaces the in-process pipeline for that call.
- Speech and model hooks for custom runtimes. `register_stt()`, `register_tts()`, and `register_llm()`, or `STT_PROVIDER`, `TTS_PROVIDER`, and `LLM_PROVIDER` set to `module:Class`. The built-in pipeline still calls Deepgram, Cartesia, and the OpenAI-compatible client directly.
- Carrier webhooks for Twilio, Telnyx, and Plivo. SignalWire keeps `/signalwire/*` so owner mode, relay, and DTMF stay on that route.
- Migration `migrations/007_open_source_hooks.sql`, plus the same column changes on startup. Adds `phone_numbers.provider`, `calls.provider`, and `agents.voice_runtime`.
- Optional installs: `requirements-livekit.txt` and `requirements-pipecat.txt`.
- Hook guide in `docs/providers.md` and sample registrations in `examples/hooks.py`.

### Removed

- Hosted billing. Balance checks, the billing ledger, usage and billing routes, and per-call debits are gone. You pay your own carrier and speech vendors.
- Supabase. `accounts.supabase_user_id` and the Supabase email client are gone. Accounts are API keys stored in Postgres.
- The unused Telnyx SDK client and the Plivo SDK dependency. Twilio, Plivo, and Telnyx talk to their HTTP APIs through `httpx`.

### Breaking

- Startup and migration `007_open_source_hooks.sql` drop `accounts.balance`, `accounts.supabase_user_id`, and `billing_ledger` on an existing database. That data is not migrated. Take a backup before upgrading a database that still has hosted billing rows.
- `/billing` and `/usage` routes are gone.
- Buying or attaching a number no longer checks a balance, and the route no longer forces a US `+1` number. SignalWire's own client still provisions US numbers only. Other carriers apply their own rules.

## [0.3.0] — 2026-08-27

`39755b1` — persistent agent relay, DTMF/IVR, owner mode, and per-agent webhooks.

- Outbound agent relay over a WebSocket, with reconnect, ack/replay, and turn-scoped context (`POST /v1/calls/{id}/context`).
- Real DTMF audio for phone menus, voicemail detection, and semantic turn-taking with barge-in.
- Owner task mode. Calls from `agents.owner_phone` capture instructions and emit `call.owner_task`.
- One signed webhook per agent, plus `GET /.well-known/agentline.json` for relay discovery.
- API version set to 0.3.0.

Follow-up on this release, with no version bump:

- 2026-08-28 — README rewritten for GitHub search, including a comparison with Vapi and Retell (`5b08e84`).

Changes after 0.2.0 that did not bump the version:

- 2026-07-05 — Skill: require a confirmation receipt before an agent places a call (`bf2313a`).
- 2026-07-08 — Skill synced to v1.13 (`fbe1625`).
- 2026-07-18 — Merged pre-action receipts (`625a882`).
- 2026-07-30 — Skill synced to v1.16 (`192bac8`).
- 2026-08-01 — Monthly number rental was added and then reverted the same day (`144d355`, `8fec3c2`, `2416956`). It is not in this tree.
- 2026-08-02 — Bearer auth accepts `al_live_` and legacy `sk_live_` keys (`9d7f99d`).

## [0.2.0] — 2026-06-22

Initial open-source release (`9ba3610`).

- FastAPI REST API for agents, phone numbers, calls, SMS, events, and voice settings.
- Built-in voice pipeline: Deepgram speech-to-text, Cartesia text-to-speech, OpenAI LLM.
- SignalWire for numbers, calls, and SMS.
- MCP server and a one-file agent skill.
- Event mailbox for agents that poll instead of holding a websocket.
- Hosted billing (balance and a ledger) and a Supabase user link. Both were removed in 0.4.0.
- Docker Compose for the API, Postgres, and Redis. MIT license.

Documentation-only commits on this release, with no version bump:

- 2026-06-22 — Removed the Railway one-click deploy button (`4efc540`).
- 2026-06-22 — Skill file: MCP usage, balance check, a bash fix, voicemail detection, and a longer billing section (`babc2a8`). The billing parts of that skill no longer apply after 0.4.0.
- 2026-07-05 — Updated the Discord invite link (`9a687bc`).
