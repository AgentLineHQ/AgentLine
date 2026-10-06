"""
AgentLine — Semantic Turn-Taking Engine

Replaces the fixed post-endpoint debounce ("wait 0.9s after every pause")
with a probability-driven decision, following the same pattern used by
LiveKit Agents' turn detector, Deepgram Agent's semantic endpointing, and
OpenAI Realtime's ``semantic_vad``:

  1. STT endpointing (Deepgram ``speech_final``) marks a *candidate* end
     of turn after a short silence — it only means the caller paused.
  2. A cheap classifier estimates P(caller finished their turn), using the
     utterance text plus recent dialogue context.  A quick lexical
     heuristic (trailing conjunction → incomplete, question → complete)
     resolves obvious cases with zero latency.
  3. The extra wait before committing the turn is a decreasing function
     of that probability:
        P ≥ 0.75  →  respond immediately   (crisp question/answer)
        P ≤ 0.25  →  wait long             (caller mid-thought: "and the…")
        in between→  scale 1.1s → 0.0s linearly; classifier unavailable → 0.55s

Because the pipeline already generates the response *speculatively* during
this window (see _schedule_response), committing early means the caller
hears the answer almost the moment they finish speaking, while callers who
are merely pausing mid-sentence keep the floor and never get talked over.

The classifier runs on every finalized STT segment (like LiveKit's turn
detector), so by the time the endpoint fires a fresh probability is usually
already computed and the decision adds zero latency.

Optional local model backends (no API calls) can be plugged into
``SemanticTurnDetector.probability``; the LLM backend needs no extra
dependencies and works on any OpenAI-compatible endpoint.
"""

import asyncio
import logging
import re
import time

from agentline.config import settings

logger = logging.getLogger(__name__)

# ── Policy tuning ──────────────────────────────────────────────────
# Probability thresholds for the piecewise wait policy.
PROB_RESPOND_NOW = 0.75      # P(end of turn) ≥ this → commit immediately
PROB_LIKELY_INCOMPLETE = 0.25  # P ≤ this → caller probably still talking

# Wait times (seconds) mapped from the probability above.
WAIT_WHEN_COMPLETE = 0.0     # crisp question/statement → no extra patience
WAIT_WHEN_INCOMPLETE = 1.1   # mid-thought trailing word → out-wait a real pause
WAIT_FALLBACK = 0.55         # classifier unavailable/slow → sensible default

# How long an endpoint decision may wait for an in-flight classification.
# Generation is speculative during this window, so this is pure decision
# latency — keep it well under the old fixed 0.9s debounce.
CLASSIFIER_BUDGET = 0.45

# Hard cap on the classifier HTTP call itself.  Longer than the decision
# budget so a slow-but-successful call still caches its result for the
# *next* endpoint instead of being wasted.
CLASSIFIER_HTTP_TIMEOUT = 2.0

# Lexical heuristic thresholds (instant, no API call).
HEURISTIC_COMPLETE = 0.9     # ≥ → obvious complete turn (e.g. clear question)

# Model used for classification — override with TURN_TAKING_MODEL in the
# environment (any OpenAI-compatible model works; smaller = faster).
TURN_TAKING_MODEL = settings.TURN_TAKING_MODEL or "gpt-4o-mini"

# A turn ending on one of these tokens usually reads as mid-sentence —
# the caller paused to think, not to hand the floor over.  Kept to clear
# connectors/prepositions/articles/auxiliaries: pronouns and words like
# "there"/"then" legitimately end complete questions ("What time is it?",
# "What happens then?"), so they are deliberately absent — ambiguous
# endings are the semantic classifier's job, not the heuristic's.
_TRAILING_INCOMPLETE_TOKENS = {
    "and", "but", "or", "so", "because", "which", "when",
    "if", "to", "for", "with", "about", "from", "the", "a", "an", "of",
    "in", "on", "at", "is", "are", "was", "were", "be", "been", "am",
    "do", "does", "did", "have", "has", "had", "will", "would", "can",
    "could", "should", "may", "might", "must", "my", "your", "his",
    "her", "our", "their", "also", "plus",
}

# Caches the compiled float-extraction pattern for classifier replies.
_FLOAT_RE = re.compile(r"\d*\.?\d+")


def heuristic_probability(utterance: str) -> float | None:
    """Instant lexical read of whether an utterance is a complete turn.

    Returns a probability, or None when the text is ambiguous (the common
    case — that's what the semantic classifier is for).
    """
    text = (utterance or "").strip()
    if not text:
        return None

    # Question → virtually always a hand-over, even a short one ("you there?")
    if text.endswith(("?", "?")):
        return 0.95

    words = text.split()
    if not words:
        return None

    # "Yeah." / "Okay." style one-word acknowledgements during a paused
    # turn usually mean more is coming ("Okay so about that…").
    if len(words) <= 2:
        return 0.2

    last = words[-1].lower().strip(".,!?;:")
    if last in _TRAILING_INCOMPLETE_TOKENS:
        return 0.05

    if text.endswith((".", ".", "!", "?")):
        return 0.7  # properly punctuated statement — probably complete

    return None


def wait_from_probability(p: float | None) -> float:
    """Map P(end of turn) to seconds of extra patience before committing."""
    if p is None:
        return WAIT_FALLBACK
    if p >= PROB_RESPOND_NOW:
        return WAIT_WHEN_COMPLETE
    if p <= PROB_LIKELY_INCOMPLETE:
        return WAIT_WHEN_INCOMPLETE
    # Linear ramp: WAIT_WHEN_INCOMPLETE at p=PROB_LIKELY_INCOMPLETE
    #              down to 0.0 at p=PROB_RESPOND_NOW.
    frac = (PROB_RESPOND_NOW - p) / (PROB_RESPOND_NOW - PROB_LIKELY_INCOMPLETE)
    return WAIT_WHEN_INCOMPLETE * frac


_CLASSIFIER_SYSTEM = """\
You are a turn-completion classifier for a live phone conversation.
The caller just paused after saying the UTTERANCE below. Decide whether
their conversational TURN is finished (they are waiting for a reply) or
incomplete (they paused mid-thought and will keep talking).

Reply with ONLY a number between 0.0 and 1.0 — the probability the turn
is FINISHED. No words, no explanation.

Guidelines:
- Finished: a question, a complete request/statement, an answer.
- Incomplete: trails off, ends on a connector (and/but/because/so…),
  an unfinished list, a clause that clearly needs more, or a bare
  acknowledgement like "okay" mid-flow.
- When unsure, answer 0.5."""


class SemanticTurnDetector:
    """Per-call semantic end-of-turn estimator.

    Feed it every finalized STT segment via ``observe_final`` (background
    classification keeps a fresh probability warm), then ask ``decide_wait``
    when the endpoint fires to learn how much extra patience (if any) the
    turn still needs.
    """

    def __init__(self, history_fn=None, model: str | None = None):
        # ``history_fn`` returns recent committed turns (list of {role,
        # content}) for dialogue context; lazily called per classification.
        self._history_fn = history_fn or (lambda: [])
        self._model = model or TURN_TAKING_MODEL
        self._text = ""          # rolling utterance text since last commit
        self._result: tuple[str, float] | None = None  # (snapshot, p)
        self._inflight: asyncio.Task | None = None
        self._inflight_text = ""  # utterance snapshot the in-flight task scored

    # ── Rolling-utterance bookkeeping ─────────────────────────────

    def observe_final(self, sentence: str) -> None:
        """Record a finalized STT segment; keep a warm probability."""
        sentence = (sentence or "").strip()
        if not sentence:
            return
        self._text = (self._text + " " + sentence).strip()
        self._kick_background_classification()

    def reseed(self, utterance: str) -> None:
        """Put a rolled-back utterance back at the front of the rolling text.

        Called when a speculative response is discarded because the caller
        resumed speaking: their continued speech must concatenate onto the
        earlier fragment, not replace it.
        """
        utterance = (utterance or "").strip()
        if not utterance:
            return
        self._text = (utterance + " " + self._text).strip() if self._text else utterance

    def reset(self) -> None:
        """Clear rolling state after a human turn is committed."""
        self._text = ""

    # ── Classification ────────────────────────────────────────────

    def _kick_background_classification(self) -> None:
        """Start a classification if none is running (cheap: ~1/s of speech)."""
        if self._inflight is not None and not self._inflight.done():
            return
        snapshot = self._text
        self._inflight_text = snapshot
        self._inflight = asyncio.create_task(self._classify(snapshot))

    async def _classify(self, utterance: str) -> tuple[str, float | None]:
        try:
            from agentline.voice.llm import client

            history = self._history_fn()
            context = ""
            for turn in list(history)[-4:]:
                role = "Caller" if turn.get("role") in ("human", "user") else "Assistant"
                content = (turn.get("content") or turn.get("text") or "").strip()
                if content:
                    context += f"{role}: {content}\n"
            user_msg = (
                (f"Recent dialogue:\n{context}\n" if context else "")
                + f"UTTERANCE: \"{utterance}\"\nProbability the turn is finished:"
            )

            resp = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _CLASSIFIER_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=4,
                temperature=0,
                timeout=CLASSIFIER_HTTP_TIMEOUT,
            )
            raw = (resp.choices[0].message.content or "").strip()
            m = _FLOAT_RE.search(raw)
            p = float(m.group()) if m else None
            if p is not None:
                p = max(0.0, min(1.0, p))
                logger.debug(
                    "turn-taking: P(finished)=%.2f for %r", p, utterance[-60:]
                )
            return utterance, p
        except Exception as e:
            logger.debug("turn-taking: classifier failed: %s", e)
            return utterance, None

    async def _fresh_probability(self, utterance: str, budget: float) -> float | None:
        """Best-effort probability for *utterance* within *budget* seconds."""
        # A cached result for exactly this text → instant.
        if self._result is not None and self._result[0] == utterance:
            return self._result[1]

        # Harvest a finished background classification before re-running.
        task = self._inflight
        if task is not None and task.done():
            if not task.cancelled() and task.exception() is None:
                self._result = task.result()
                if self._result[0] == utterance:
                    return self._result[1]
            task = None
        # An in-flight task scoring a STALE snapshot (the caller kept
        # talking) can't answer this question — start a fresh one for the
        # full utterance instead of waiting out the doomed task.
        if task is None or self._inflight_text != utterance:
            task = asyncio.create_task(self._classify(utterance))
            self._inflight = task
            self._inflight_text = utterance
        try:
            snapshot, p = await asyncio.wait_for(
                asyncio.shield(task), timeout=budget
            )
        except Exception:
            # Timeout (or rare failure): the shielded task keeps running and
            # its result is harvested at the next endpoint — nothing wasted.
            return None
        self._result = (snapshot, p)
        if snapshot == utterance:
            return p
        # Snapshot predates the final words (caller added more before the
        # endpoint) — treat as stale but usable only if it matched fully.
        return None

    # ── Decision ──────────────────────────────────────────────────

    async def decide_wait(self, utterance: str) -> float:
        """Seconds of extra patience this endpoint needs before committing.

        Clear questions commit instantly from the lexical heuristic; every
        other case waits at most CLASSIFIER_BUDGET for the semantic
        probability and falls back to the heuristic prior (which leans
        toward patience — never talking over people is the priority).
        """
        # Synthetic sentinels (outbound silence fallback, markers) are
        # never caller speech — no patience needed.
        if (utterance or "").lstrip().startswith("["):
            return 0.0

        h = heuristic_probability(utterance)
        if h is not None and h >= HEURISTIC_COMPLETE:
            return 0.0

        p = await self._fresh_probability(utterance, CLASSIFIER_BUDGET)
        if p is not None:
            wait = wait_from_probability(p)
            logger.debug(
                "turn-taking: P(finished)=%.2f → extra wait %.2fs for %r",
                p, wait, utterance[-60:],
            )
            return wait

        # No semantic signal in budget — fall back to the lexical read
        # (trailing connector → long wait; punctuated statement → short).
        if h is not None:
            return wait_from_probability(h)
        return WAIT_FALLBACK
