"""PCM audio helpers: formats, frame maths, energy, mu-law codec, cheap resampling.

Everything here is pure Python on purpose: the pipeline must run on a small CPU
box with no native audio dependencies. Frame sizes are tiny (20 ms) so plain
loops are fast enough for many concurrent calls.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class AudioFormat:
    """Signed 16-bit little-endian mono PCM unless ``encoding`` says otherwise."""

    sample_rate: int = 16000
    channels: int = 1
    encoding: str = "linear16"  # or "mulaw"

    @property
    def bytes_per_sample(self) -> int:
        return 1 if self.encoding == "mulaw" else 2

    def bytes_for(self, seconds: float) -> int:
        return int(round(seconds * self.sample_rate)) * self.channels * self.bytes_per_sample

    def seconds_for(self, nbytes: int) -> float:
        return nbytes / (self.sample_rate * self.channels * self.bytes_per_sample)


PCM16_16K = AudioFormat(16000, 1, "linear16")
PCM16_8K = AudioFormat(8000, 1, "linear16")
MULAW_8K = AudioFormat(8000, 1, "mulaw")

FRAME_MS = 20


def frame_bytes(fmt: AudioFormat, frame_ms: int = FRAME_MS) -> int:
    return fmt.bytes_for(frame_ms / 1000.0)


def rms_pcm16(frame: bytes) -> float:
    """Root-mean-square amplitude of a PCM16 frame, normalised to 0..1."""
    n = len(frame) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", frame[: n * 2])
    acc = 0
    for s in samples:
        acc += s * s
    return math.sqrt(acc / n) / 32768.0


def silence(fmt: AudioFormat, seconds: float) -> bytes:
    if fmt.encoding == "mulaw":
        return b"\xff" * fmt.bytes_for(seconds)  # 0xFF is mu-law zero
    return b"\x00" * fmt.bytes_for(seconds)


def tone(fmt: AudioFormat, seconds: float, freq: float = 220.0, amplitude: float = 0.3) -> bytes:
    """A sine tone; used by mock providers as stand-in 'speech' with real energy."""
    n = int(round(seconds * fmt.sample_rate))
    out = bytearray()
    peak = int(amplitude * 32767)
    for i in range(n):
        v = int(peak * math.sin(2 * math.pi * freq * i / fmt.sample_rate))
        out += struct.pack("<h", v)
    if fmt.encoding == "mulaw":
        return pcm16_to_mulaw(bytes(out))
    return bytes(out)


# --- mu-law (G.711) ----------------------------------------------------------
# Pure-Python codec so we do not depend on the deprecated ``audioop`` module.

_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635


def _linear_to_mulaw_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > _MULAW_CLIP:
        sample = _MULAW_CLIP
    sample += _MULAW_BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_ENC_TABLE = bytes(_linear_to_mulaw_sample(i - 32768) for i in range(65536))


def _mulaw_to_linear_sample(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + _MULAW_BIAS) << exponent
    sample -= _MULAW_BIAS
    return -sample if sign else sample


_DEC_TABLE = [_mulaw_to_linear_sample(i) for i in range(256)]


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return bytes(_ENC_TABLE[s + 32768] for s in samples)


def mulaw_to_pcm16(mu: bytes) -> bytes:
    return struct.pack(f"<{len(mu)}h", *(_DEC_TABLE[b] for b in mu))


# --- resampling ---------------------------------------------------------------


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resampler. Adequate for 8k<->16k speech; not hi-fi."""
    if src_rate == dst_rate:
        return pcm
    n = len(pcm) // 2
    if n == 0:
        return b""
    src = struct.unpack(f"<{n}h", pcm[: n * 2])
    m = int(n * dst_rate / src_rate)
    out = []
    ratio = src_rate / dst_rate
    for i in range(m):
        pos = i * ratio
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j + 1 < n else a
        out.append(int(a + (b - a) * frac))
    return struct.pack(f"<{m}h", *out)
