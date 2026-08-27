"""
AgentLine — DTMF (Touch-Tone) Generator
Generates real DTMF (Dual-Tone Multi-Frequency) audio as G.711 μ-law bytes
ready to stream through the same <Connect><Stream> path used for TTS.

Why this exists:
  IVR phone menus ("Press 1 for sales, 2 for support") listen for DTMF tones,
  NOT spoken words. Speaking "one" into the call via TTS does nothing — the
  agent gets stuck looping the menu. This module turns a digit string like
  "1ww2" into the actual two-tone audio an IVR detector recognises.

Output format: pcm_mulaw, 8000 Hz, mono — identical to Cartesia TTS output,
so it can be fed straight into the existing provider `send_audio` helper.

Pure Python (no `audioop`, which was removed in Python 3.13). The G.711
encoding is the canonical Sun/ITU-T algorithm (μ = 255).
"""

import math
import logging

logger = logging.getLogger(__name__)

# ── Sample format ────────────────────────────────────────────────
SAMPLE_RATE = 8000  # Hz — telephony standard

# ── DTMF timing (robust defaults; detectable by virtually all IVRs) ──
TONE_MS = 200        # length of each digit's tone
GAP_MS = 100         # silence between digits
W_PAUSE_MS = 500     # 'w' marker = half-second pause (LaML convention)

# Per-tone amplitude. Two sines are summed, so peak ≈ 2×AMPLITUDE = 30000,
# comfortably below the μ-law clip threshold of 32635 (no distortion).
AMPLITUDE = 15000

# ── DTMF frequency table (ITU-T Q.23) ────────────────────────────
#   Each key = (low group freq, high group freq)
_DTMF_FREQS = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}

# ── G.711 μ-law encoding (canonical Sun/ITU-T reference algorithm) ──
_BIAS = 0x84   # 132
_CLIP = 32635

# Segment lookup: maps the top 8 magnitude bits → segment number (0-7).
_EXP_LUT = (
    0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3,
    4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
    5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
    5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
    6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
    6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
    6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
    6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
)


def _lin_to_mulaw(sample: int) -> int:
    """Encode a 16-bit signed PCM sample to a transmitted G.711 μ-law byte (0-255).

    Output already includes the final bit-inversion, so a silent sample (0)
    encodes to 0xFF and max positive encodes to 0x80 — matching what Cartesia
    and the telephony providers expect.
    """
    sign = (sample >> 8) & 0x80
    if sign:
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS
    exponent = _EXP_LUT[(sample >> 7) & 0xFF]
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _gen_tone(freq_low: float, freq_high: float, duration_ms: int) -> bytes:
    """Synthesize a two-tone DTMF signal and μ-law encode it."""
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    out = bytearray(n_samples)
    two_pi_low = 2.0 * math.pi * freq_low / SAMPLE_RATE
    two_pi_high = 2.0 * math.pi * freq_high / SAMPLE_RATE
    for i in range(n_samples):
        s = AMPLITUDE * math.sin(two_pi_low * i) + AMPLITUDE * math.sin(two_pi_high * i)
        out[i] = _lin_to_mulaw(int(s))
    return bytes(out)


def _gen_silence(duration_ms: int) -> bytes:
    """μ-law silence (sample 0 → byte 0xFF)."""
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    return b"\xff" * n_samples


def generate_dtmf(digits: str) -> bytes:
    """Generate pcm_mulaw@8kHz DTMF audio for a digit string.

    Supported characters (case-insensitive):
      0-9, *, #, A-D  →  a DTMF tone (TONE_MS long, followed by GAP_MS silence)
      'w' or 'W'      →  half-second pause (W_PAUSE_MS)
      ','             →  short pause (GAP_MS)

    Examples:
      "1"        →  press 1                    (~300ms)
      "1ww2"     →  1, pause, pause, 2         (~1.6s)
      "*69"      →  star-six-nine              (~900ms)

    Unknown characters are skipped silently.
    """
    if not digits:
        return b""

    parts: list[bytes] = []
    for ch in digits.upper():
        if ch == "W":
            parts.append(_gen_silence(W_PAUSE_MS))
        elif ch == ",":
            parts.append(_gen_silence(GAP_MS))
        elif ch in _DTMF_FREQS:
            low, high = _DTMF_FREQS[ch]
            parts.append(_gen_tone(low, high, TONE_MS))
            parts.append(_gen_silence(GAP_MS))
        # else: ignore unknown chars

    audio = b"".join(parts)
    logger.debug("Generated DTMF audio for %r: %d bytes (%.0fms)",
                 digits, len(audio), len(audio) * 1000 / SAMPLE_RATE)
    return audio
