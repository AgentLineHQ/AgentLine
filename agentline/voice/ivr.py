"""
Outbound IVR / voicemail policy.

The hosted LLM is asked to emit [DTMF:X] or [VOICEMAIL_DETECTED], but it
reliably fails in two ways that burn whole calls:

  1. It treats a voicemail greeting ("12418174 is not available") as a
     menu and presses 0 — the prompt's "operator fallback".
  2. On a mailbox recovery menu ("To disconnect, press 1. To record your
     message, press 2.") it keeps pressing 0, which is not even offered.

This module is a deterministic safety net in front of / after the LLM:
classify the utterance, extract the keys that were actually spoken, and
correct (or skip) the model when it would press a key that cannot work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ── Digit tokens ──────────────────────────────────────────────────

_DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "star": "*",
    "asterisk": "*",
    "pound": "#",
    "hash": "#",
}

_PRESS_TOKEN = (
    r"(?:\d{1,6}|[0-9*#]|zero|oh|one|two|three|four|five|six|seven|"
    r"eight|nine|star|asterisk|pound|hash)"
)

# "press 1", "dial 0", "enter the pound key"
_PRESS_RE = re.compile(
    rf"(?:press|dial|enter|touch|hit|push)\s+(?:the\s+)?({_PRESS_TOKEN})\b",
    re.IGNORECASE,
)

# High-confidence mailbox language. Deliberately NOT a bare "not available"
# — a live person saying "I'm not available Tuesday" must stay live.
_VM_GREETING_RE = re.compile(
    r"\b(?:"
    r"(?:is|are|was|were) not available|is unavailable|are unavailable|"
    r"can'?t come to the phone|cannot come to the phone|"
    r"leave (?:a |your )?message|after the (?:beep|tone)|"
    r"you(?:'ve| have) reached|voicemail|voice mail|mailbox|"
    r"no one is available|person you(?:'re| are) trying to reach|"
    r"please record your message"
    r")\b",
    re.IGNORECASE,
)

_FIRST_PERSON_RE = re.compile(
    r"^\s*(?:sorry[,.]?\s+)?i(?:'m| am)\b",
    re.IGNORECASE,
)

# Mailbox asked for a key to record or hang up (not a business IVR).
_VM_RECORD_MENU_RE = re.compile(
    r"(?:"
    r"record your message|to record\b|press .{0,24}record|"
    r"leave (?:a |your )?message.{0,48}press|"
    r"we (?:didn'?t|did not) get your message|"
    r"to disconnect.{0,24}press|press .{0,24}disconnect"
    r")",
    re.IGNORECASE,
)

_LIVE_HUMAN_RE = re.compile(
    r"\b(?:"
    r"who (?:is this|am i speaking|'?s calling)|"
    r"how can i help|can i (?:help you|take a message)|"
    r"what(?:'s| is) this (?:about|regarding)|"
    r"may i (?:ask|help|take)|"
    r"who(?:'s| is) calling"
    r")\b",
    re.IGNORECASE,
)

_SPEECH_OPTION_RE = re.compile(
    r"\b(?:say|speak|state)\b",
    re.IGNORECASE,
)

_LIVE_GREETING_ONLY_RE = re.compile(
    r"^\s*(?:hello|hi|hey|yeah|yes|yo)[?.!]?\s*$",
    re.IGNORECASE,
)

_RECORD_HINTS = (
    "record",
    "leave a message",
    "leave your message",
    "leave me a message",
)
_DISCONNECT_HINTS = (
    "disconnect",
    "hang up",
    "end the call",
    "end this call",
    "to end",
)
_OPERATOR_HINTS = (
    "operator",
    "attendant",
    "representative",
    "reception",
    "receptionist",
    "a human",
    "real person",
)
_REPEAT_HINTS = ("repeat", "replay", "hear the menu", "hear this menu")


@dataclass(frozen=True)
class MenuOption:
    digit: str
    label: str  # record | disconnect | operator | repeat | other
    clause: str


@dataclass(frozen=True)
class OutboundAction:
    """What the pipeline should do for this outbound turn."""

    kind: str  # "voicemail" | "dtmf" | "passthrough" | "drop"
    digits: str | None = None
    then_voicemail: bool = False
    reason: str = ""


def normalize_digit_token(token: str) -> str:
    """Map a spoken/typed key token to a DTMF character or digit string."""
    raw = (token or "").strip().lower()
    if raw in _DIGIT_WORDS:
        return _DIGIT_WORDS[raw]
    if raw in {"*", "#"} or raw.isdigit():
        return raw
    return ""


def tone_digits(digits: str | None) -> str:
    """Strip pause markers so '0ww' and '0' compare equal."""
    if not digits:
        return ""
    return "".join(ch for ch in digits.upper() if ch in "0123456789*#ABCD")


def is_live_greeting(text: str) -> bool:
    """Return whether a short utterance confidently indicates a live callee."""
    return bool(_LIVE_GREETING_ONLY_RE.fullmatch(text or ""))


def extract_menu_options(text: str) -> list[MenuOption]:
    """Return the keys the prompt actually offered, with a coarse label."""
    if not text:
        return []

    options: list[MenuOption] = []
    seen: set[str] = set()
    for match in _PRESS_RE.finditer(text):
        digit = normalize_digit_token(match.group(1))
        if not digit or digit in seen:
            continue
        seen.add(digit)
        clause = _clause_around(text, match.start(), match.end())
        options.append(MenuOption(digit=digit, label=_label_clause(clause), clause=clause))
    return options


def classify_outbound_utterance(text: str) -> str:
    """Classify a callee utterance.

    Returns one of: voicemail_greeting, voicemail_record_menu, ivr, live.
    """
    utterance = (text or "").strip()
    if not utterance:
        return "live"

    options = extract_menu_options(utterance)
    record_menu = bool(_VM_RECORD_MENU_RE.search(utterance))
    vm_greeting = bool(_VM_GREETING_RE.search(utterance)) and not _FIRST_PERSON_RE.search(
        utterance
    )
    live_human = bool(_LIVE_HUMAN_RE.search(utterance))

    # A mailbox key menu always wins — "press 2 to record" is not a sales IVR.
    if options and record_menu:
        return "voicemail_record_menu"

    # "You've reached… press 1 to leave a message" — still a mailbox.
    if options and vm_greeting and any(opt.label == "record" for opt in options):
        return "voicemail_record_menu"

    if vm_greeting and not options:
        # A question ("is not available — would you like a callback?") is a
        # receptionist, not a carrier mailbox.
        if "?" in utterance:
            return "live"
        return "voicemail_greeting"

    # Live-human phrases beat a bare "hello" voicemail opener only when
    # there is no mailbox language at all.
    if live_human and not vm_greeting and not record_menu:
        return "live"

    if options:
        return "ivr"

    return "live"


def build_outbound_turn_hint(
    utterance: str,
    last_dtmf: str | None,
    has_voicemail_message: bool,
) -> str:
    """Short [SYSTEM: …] suffix so the LLM sees the offered keys this turn."""
    kind = classify_outbound_utterance(utterance)
    options = extract_menu_options(utterance)
    last = tone_digits(last_dtmf)

    if kind == "voicemail_greeting":
        return (
            "[SYSTEM: This is a voicemail greeting, not a phone menu. "
            "Output ONLY [VOICEMAIL_DETECTED]. Do not press any DTMF keys.]"
        )

    if kind == "voicemail_record_menu":
        offered = _format_options(options)
        goal = (
            "Press the key that records/leaves a message, then stop."
            if has_voicemail_message
            else "No voicemail message is configured — press the disconnect key."
        )
        failed = (
            f" Your last press ({last}) was not accepted — do not press it again."
            if last
            else ""
        )
        return (
            f"[SYSTEM: Voicemail key menu. Offered keys: {offered}. {goal}"
            f" Never press a key that was not listed.{failed}]"
        )

    if kind == "ivr" and options:
        offered = _format_options(options)
        failed = (
            f" Your last press ({last}) was ignored — pick a DIFFERENT listed key."
            if last
            else ""
        )
        return (
            f"[SYSTEM: Phone menu. Offered keys: {offered}. "
            f"Output ONLY [DTMF:X] using a listed key. "
            f"Never press 0 unless 0 was listed.{failed}]"
        )

    return ""


def resolve_outbound_action(
    utterance: str,
    *,
    llm_digits: str | None = None,
    llm_voicemail: bool = False,
    last_dtmf: str | None = None,
    has_voicemail_message: bool = True,
) -> OutboundAction:
    """Decide voicemail vs DTMF vs let-the-LLM-speak.

    Called twice per turn:
      * before the LLM, with no decision — force mailbox actions that
        must not wait on the model (greeting / record-key menu).
      * after the LLM emits a marker (or speech), to correct a bad key.
    """
    kind = classify_outbound_utterance(utterance)
    options = extract_menu_options(utterance)
    last = tone_digits(last_dtmf)
    wanted = tone_digits(llm_digits)

    if kind == "voicemail_greeting":
        # Speaking or pressing keys during a mailbox greeting makes the
        # carrier say "we didn't get your message". Always detect VM.
        return OutboundAction(kind="voicemail", reason="voicemail_greeting")

    if kind == "voicemail_record_menu":
        return _resolve_record_menu(
            options,
            wanted=wanted,
            last=last,
            has_voicemail_message=has_voicemail_message,
            llm_digits=llm_digits,
        )

    if kind == "ivr":
        return _resolve_ivr(
            options,
            utterance=utterance,
            llm_digits=llm_digits,
            wanted=wanted,
            last=last,
            llm_voicemail=llm_voicemail,
        )

    # Live human (or unclassified). Honour an explicit LLM marker; do not
    # invent one. A bare "Hello?" plus [DTMF:0] is the 0-fallback hallucination
    # — drop it rather than tone-blast a person.
    if wanted and is_live_greeting(utterance):
        return OutboundAction(kind="drop", reason="dtmf_on_live_greeting")
    if llm_voicemail:
        return OutboundAction(kind="voicemail", reason="llm_voicemail")
    if wanted:
        return OutboundAction(kind="dtmf", digits=llm_digits, reason="llm_dtmf")
    return OutboundAction(kind="passthrough", reason="live")


def _resolve_record_menu(
    options: list[MenuOption],
    *,
    wanted: str,
    last: str,
    has_voicemail_message: bool,
    llm_digits: str | None,
) -> OutboundAction:
    record = _first_labeled(options, "record")
    disconnect = _first_labeled(options, "disconnect")
    offered = [opt.digit for opt in options]

    if has_voicemail_message and record:
        # Honour the LLM only if it picked a listed key that isn't the
        # one that just failed. Still leave a message after a record key.
        if wanted and wanted in offered and wanted != last:
            return OutboundAction(
                kind="dtmf",
                digits=llm_digits or wanted,
                then_voicemail=(wanted == record),
                reason="record_menu_honour_llm",
            )
        return OutboundAction(
            kind="dtmf",
            digits=record,
            then_voicemail=True,
            reason="record_menu_press_record",
        )

    if disconnect and (not has_voicemail_message or not record):
        return OutboundAction(
            kind="dtmf",
            digits=disconnect,
            reason="record_menu_disconnect",
        )

    if wanted and wanted in offered and wanted != last:
        return OutboundAction(
            kind="dtmf",
            digits=llm_digits or wanted,
            then_voicemail=False,
            reason="record_menu_listed_key",
        )

    fallback = _next_offered(offered, last)
    if fallback:
        return OutboundAction(
            kind="dtmf",
            digits=fallback,
            reason="record_menu_fallback",
        )
    return OutboundAction(kind="voicemail", reason="record_menu_no_keys")


def _resolve_ivr(
    options: list[MenuOption],
    *,
    utterance: str,
    llm_digits: str | None,
    wanted: str,
    last: str,
    llm_voicemail: bool,
) -> OutboundAction:
    offered = [opt.digit for opt in options]
    allows_speech = bool(_SPEECH_OPTION_RE.search(utterance or ""))

    # A sales/support tree is not voicemail. Drop a mistaken VM marker
    # unless a record key is sitting on the same prompt.
    if llm_voicemail and not any(opt.label == "record" for opt in options):
        llm_voicemail = False

    if wanted:
        if wanted in offered and wanted != last:
            return OutboundAction(
                kind="dtmf",
                digits=llm_digits or wanted,
                then_voicemail=any(
                    opt.digit == wanted and opt.label == "record" for opt in options
                ),
                reason="ivr_llm_valid",
            )
        # Same key the menu just rejected, or a key that was never offered.
        replacement = _next_offered(offered, last or wanted)
        if replacement:
            return OutboundAction(
                kind="dtmf",
                digits=replacement,
                reason="ivr_corrected_invalid_or_repeat",
            )

    if llm_voicemail:
        return OutboundAction(kind="voicemail", reason="ivr_with_record")

    # No LLM key yet. Don't steal a spoken option ("say yes or press 1").
    if not wanted and allows_speech:
        return OutboundAction(kind="passthrough", reason="ivr_allows_speech")

    # No LLM key on a press-only menu: pick operator if listed, else a
    # key that isn't the one that just failed. Used both as a pre-LLM
    # no-op (passthrough when we refuse to guess) and as a correction
    # when the model spoke instead of pressing.
    # First pass (no LLM key yet): let the model pick a purpose-matching key.
    return OutboundAction(kind="passthrough", reason="ivr_await_llm")


def _first_labeled(options: list[MenuOption], label: str) -> str | None:
    for opt in options:
        if opt.label == label:
            return opt.digit
    return None


def _next_offered(offered: list[str], avoid: str) -> str | None:
    for digit in offered:
        if digit != avoid:
            return digit
    return offered[0] if offered else None


def _clause_around(text: str, start: int, end: int) -> str:
    lo = start
    while lo > 0 and text[lo - 1] not in ".!?;":
        lo -= 1
    hi = end
    while hi < len(text) and text[hi] not in ".!?;":
        hi += 1
    return text[lo:hi].strip()


def _label_clause(clause: str) -> str:
    lower = clause.lower()
    if any(hint in lower for hint in _RECORD_HINTS):
        return "record"
    if any(hint in lower for hint in _DISCONNECT_HINTS):
        return "disconnect"
    if any(hint in lower for hint in _OPERATOR_HINTS):
        return "operator"
    if any(hint in lower for hint in _REPEAT_HINTS):
        return "repeat"
    return "other"


def _format_options(options: list[MenuOption]) -> str:
    parts = []
    for opt in options:
        if opt.label != "other":
            parts.append(f"{opt.digit}={opt.label}")
        else:
            parts.append(opt.digit)
    return ", ".join(parts) if parts else "(none parsed)"
