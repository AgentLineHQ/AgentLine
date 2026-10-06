"""
AgentLine — Owner Task Mode (shared by inbound + outbound paths).

When the account owner interacts with their own agent — inbound (owner
calls the agent's number) or outbound (agent calls the owner's number) —
the voice AI enters "task mode": it records the owner's spoken instructions
as a task instead of conducting a normal conversation.

The task is executed by a SEPARATE background agent AFTER the call ends
(driven by the `call.owner_task` event), NOT during the call. The prompts
below make that boundary explicit so the voice AI never promises real-time
action it cannot perform.

This module is intentionally dependency-free (no DB, no config, no FastAPI)
so it can be unit-tested in isolation and imported by any router without
circular-import risk.
"""

from typing import Mapping, Optional, Tuple

# Spoken at the start of every owner-mode call.
OWNER_MODE_GREETING = "Hey boss, what would you like me to do?"

# Sentinel prefix stored at the top of calls.system_prompt. The hangup
# handlers check for this to decide whether to emit a `call.owner_task`
# event (vs. a plain `call.completed`). MUST stay in sync with the first
# line of OWNER_MODE_PROMPT below.
OWNER_MODE_SENTINEL = "OWNER MODE"

OWNER_MODE_PROMPT = f"""\
{OWNER_MODE_SENTINEL} — The person on this call is your OWNER (the account holder who controls you).
Treat them like your boss. Speak naturally — use fillers, mirror their mood, sound human — but stay focused and concise.

STEP 1 — CLASSIFY what the owner just said. Is it a genuine TASK, or NOT?

A TASK is a clear, actionable instruction for you to carry out LATER, e.g.
"call John and remind him", "text the team the new time", "set up a support
number", "find out our spend last month", "reschedule the meeting".
Action verb + object + (usually) a target = a task.

NOT a task:
 - Greetings / small talk ("hey", "how are you", "you there?")
 - Questions about you or the system ("what can you do?", "who are you?")
 - Asking your opinion or status ("did that call go through?")
 - Vague statements with no action ("so, about that project...")
 - The owner just testing the line

STEP 2 — Respond based on the classification:

IF IT IS A TASK:
  1. If anything is unclear (who, what, when), ask ONE short clarifying question first.
  2. Once understood, briefly summarize the task back to confirm you got it right.
  3. Confirm you'll take care of it — and VARY your wording. Do NOT repeat the same
     phrase every time. Sound like a capable human assistant, e.g.
     "Got it, I'll get on that once we're done here",
     "Sure thing, I'll handle that after we hang up",
     "Makes sense — I'll take care of that".

IF IT IS NOT A TASK:
  - Respond naturally and helpfully. Answer the question, chat briefly, or simply
    ask what they'd like you to do.
  - NEVER fabricate a task. Do NOT say "I'll complete the task" or "Got it, I'll
    get that done" when no task was actually given — that is robotic and wrong.

CAPABILITY BOUNDARY (always true):
 - You CANNOT execute the task during this call — you have no tools, no phone, and
   no ability to act on anything in real time.
 - Your only job during the call is to understand and confirm. Execution happens
   AFTER you hang up, handled by a separate background agent that reads this call's
   transcript.
 - NEVER promise real-time action. If the owner says "call John right now", reply
   something like: "I can't do it while we're talking, but I'll handle it the moment
   we hang up."
 - Don't ask the owner to stay on the line for results — they can hang up once the
   task is confirmed.
"""


def _agent_field(agent, field: str) -> Optional[str]:
    if not agent:
        return None
    if hasattr(agent, "get"):
        return agent.get(field)
    # asyncpg.Record supports mapping access but not .get in older versions
    try:
        return agent[field]
    except (KeyError, IndexError, TypeError):
        return None


def is_owner_number(agent, number: Optional[str]) -> bool:
    """
    Return True if `number` matches the agent's registered `owner_phone`.

    Comparison ignores a leading '+' on either side so that '+1212...' and
    '1212...' are treated as equal. Handles None/empty safely.
    """
    if not number:
        return False
    owner_phone = _agent_field(agent, "owner_phone")
    if not owner_phone:
        return False
    return number.lstrip("+") == owner_phone.lstrip("+")


def build_owner_prompt(agent) -> str:
    """
    Build the system prompt for an owner-mode call.

    Prepends OWNER_MODE_PROMPT (which carries the sentinel) to the agent's
    configured base prompt, so the owner-mode rules win while the agent's
    personality/context is still available.
    """
    base = _agent_field(agent, "system_prompt") or ""
    return OWNER_MODE_PROMPT + "\n" + base


def resolve_outbound_owner_overrides(
    agent,
    to_number: str,
    body_system_prompt: Optional[str],
    body_initial_greeting: Optional[str],
) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Decide system_prompt + initial_greeting for an OUTBOUND call,
    applying owner-mode overrides when the destination is the owner.

    Priority (matches the documented chain: per-call > owner-mode >
    agent-default, resolved further in the WebSocket stream handler):
      1. Explicit per-call overrides from the POST body always win — the
         caller may legitimately want a custom prompt even when dialing
         their own number.
      2. Otherwise, if to_number is the owner, swap in the owner-mode
         prompt + greeting ("Hey boss...").
      3. Otherwise fall back to the agent default (system_prompt) or leave
         greeting None so the WebSocket handler fills in the agent default.

    Returns (system_prompt, initial_greeting, is_owner).
    """
    is_owner = is_owner_number(agent, to_number)

    if is_owner:
        system_prompt = (
            body_system_prompt if body_system_prompt is not None
            else build_owner_prompt(agent)
        )
        greeting = (
            body_initial_greeting if body_initial_greeting is not None
            else OWNER_MODE_GREETING
        )
        return system_prompt, greeting, True

    # Non-owner: preserve previous behaviour exactly.
    system_prompt = (
        body_system_prompt if body_system_prompt is not None
        else _agent_field(agent, "system_prompt")
    )
    # Leave greeting as supplied (None when unset) — the WebSocket stream
    # handler resolves it to agent.initial_greeting at connect time.
    return system_prompt, body_initial_greeting, False
