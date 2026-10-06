"""G.711 μ-law conversion for 8 kHz telephony audio.

The LiveKit bridge uses this so the optional LiveKit extra does not also
need the stdlib ``audioop`` module.
"""

_BIAS = 0x84
_CLIP = 32635


def ulaw_to_pcm16(data: bytes) -> bytes:
    out = bytearray(len(data) * 2)
    for i, raw in enumerate(data):
        value = (~raw) & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        if sign:
            sample = -sample
        if sample > 32767:
            sample = 32767
        elif sample < -32768:
            sample = -32768
        out[i * 2] = sample & 0xFF
        out[i * 2 + 1] = (sample >> 8) & 0xFF
    return bytes(out)


def pcm16_to_ulaw(data: bytes) -> bytes:
    count = len(data) // 2
    out = bytearray(count)
    for i in range(count):
        sample = int.from_bytes(data[i * 2:i * 2 + 2], "little", signed=True)
        sign = 0x80 if sample < 0 else 0
        if sample < 0:
            sample = -sample
        if sample > _CLIP:
            sample = _CLIP
        sample += _BIAS
        exponent = 7
        mask = 0x4000
        while exponent > 0 and (sample & mask) == 0:
            exponent -= 1
            mask >>= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        out[i] = (~(sign | (exponent << 4) | mantissa)) & 0xFF
    return bytes(out)
