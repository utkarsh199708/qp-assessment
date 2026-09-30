import struct

from voice_agent.audio import (
    MULAW_8K,
    PCM16_8K,
    PCM16_16K,
    frame_bytes,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_pcm16,
    rms_pcm16,
    silence,
    tone,
)
from voice_agent.vad import Endpointer, EndpointerConfig, EnergyVAD, VadEvent


def test_formats_and_frames():
    assert frame_bytes(PCM16_16K) == 640
    assert frame_bytes(PCM16_8K) == 320
    assert frame_bytes(MULAW_8K) == 160
    assert PCM16_16K.seconds_for(32000) == 1.0
    assert len(silence(MULAW_8K, 1.0)) == 8000


def test_mulaw_roundtrip_is_close():
    pcm = tone(PCM16_8K, 0.1, freq=300, amplitude=0.5)
    back = mulaw_to_pcm16(pcm16_to_mulaw(pcm))
    assert len(back) == len(pcm)
    a = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    b = struct.unpack(f"<{len(back) // 2}h", back)
    err = max(abs(x - y) for x, y in zip(a, b, strict=True))
    assert err < 1200  # 8-bit companding, ~3-4% of full scale worst case
    assert mulaw_to_pcm16(b"\xff") == b"\x00\x00"  # mu-law zero


def test_resample_lengths():
    pcm8 = tone(PCM16_8K, 0.5)
    pcm16 = resample_pcm16(pcm8, 8000, 16000)
    assert len(pcm16) == 2 * len(pcm8)
    assert resample_pcm16(pcm16, 16000, 8000) == resample_pcm16(pcm16, 16000, 8000)
    assert resample_pcm16(pcm8, 8000, 8000) is pcm8


def test_rms():
    assert rms_pcm16(silence(PCM16_16K, 0.02)) == 0.0
    assert 0.2 < rms_pcm16(tone(PCM16_16K, 0.02, amplitude=0.3)) < 0.3


def _frames(audio: bytes, n: int = 640):
    return [audio[i : i + n] for i in range(0, len(audio) - n + 1, n)]


def test_endpointer_segments_and_gating():
    cfg = EndpointerConfig(start_ms=60, end_ms=300, preroll_ms=200, postroll_ms=100)
    ep = Endpointer(EnergyVAD(), cfg)
    stream = (
        _frames(silence(PCM16_16K, 1.0)) + _frames(tone(PCM16_16K, 1.0)) + _frames(silence(PCM16_16K, 1.0))
    )
    events = []
    forwarded = 0
    for i, f in enumerate(stream):
        fw, ev = ep.push(f)
        forwarded += len(fw)
        if ev:
            events.append((i, ev))
    assert [e for _, e in events] == [VadEvent.SPEECH_START, VadEvent.SPEECH_END]
    start_i, end_i = events[0][0], events[1][0]
    assert start_i == 50 + 2  # third speech frame (60 ms) opens the segment
    assert end_i == 100 + 14  # the 15th silent frame (300 ms) closes it
    # forwarded = pre-roll window (10 frames, of which 2 are the first speech frames) + remaining speech (48)
    #             + silence inside end_ms (15) + post-roll (5)
    assert forwarded == 10 + 48 + 15 + 5
    assert forwarded < len(stream)


def test_endpointer_ignores_clicks():
    ep = Endpointer(EnergyVAD(), EndpointerConfig(start_ms=60))
    click = _frames(tone(PCM16_16K, 0.04))  # 2 frames of noise
    quiet = _frames(silence(PCM16_16K, 0.5))
    events = [ep.push(f)[1] for f in quiet + click + quiet]
    assert all(e is None for e in events)
