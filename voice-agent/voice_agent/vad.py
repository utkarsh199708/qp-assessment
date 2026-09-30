"""Voice activity detection and endpointing.

The default detector is an adaptive-threshold energy VAD: no model download, a
few microseconds per 20 ms frame, good enough for telephone-band speech against a
steady noise floor. Anything better (Silero VAD, a semantic turn detector) plugs
in through the same :class:`FrameClassifier` protocol; the endpointer state
machine is shared.

Why endpointing lives here and not in the STT provider: the pipeline uses the
speech/no-speech decision for *three* cost levers at once -
  1. gating which audio is sent to (and billed by) the STT provider,
  2. deciding when the user turn is over so the LLM starts as early as possible,
  3. detecting barge-in so agent audio is cut and no more TTS is bought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .audio import rms_pcm16


class FrameClassifier(Protocol):
    def is_speech(self, frame: bytes) -> bool: ...


class EnergyVAD:
    """Adaptive energy detector.

    Keeps a slowly-moving noise-floor estimate; a frame is speech when its RMS is
    above ``max(min_threshold, floor * ratio)``. The floor only adapts on frames
    classified as silence so speech does not raise it.
    """

    def __init__(self, min_threshold: float = 0.01, ratio: float = 3.0, adapt: float = 0.05):
        self.min_threshold = min_threshold
        self.ratio = ratio
        self.adapt = adapt
        self.floor = min_threshold / ratio

    def is_speech(self, frame: bytes) -> bool:
        level = rms_pcm16(frame)
        threshold = max(self.min_threshold, self.floor * self.ratio)
        speech = level > threshold
        if not speech:
            self.floor += (level - self.floor) * self.adapt
        return speech


class VadEvent(Enum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass
class EndpointerConfig:
    frame_ms: int = 20
    start_ms: int = 60  # consecutive speech needed to open a segment (rejects clicks)
    end_ms: int = 500  # trailing silence that closes the segment (the "endpoint")
    preroll_ms: int = 240  # audio kept before SPEECH_START and sent to STT
    postroll_ms: int = 200  # audio after SPEECH_END still sent to STT


@dataclass
class Endpointer:
    """Turns per-frame speech flags into segments with pre-roll and post-roll.

    ``push`` returns the frames that should be forwarded to STT for this input
    frame (possibly several when the pre-roll buffer is flushed, possibly none
    while gating) and any event that fired.
    """

    classifier: FrameClassifier
    config: EndpointerConfig = field(default_factory=EndpointerConfig)
    in_speech: bool = False
    _speech_run: int = 0
    _silence_run: int = 0
    _preroll: list[bytes] = field(default_factory=list)
    _postroll_left: int = 0

    def push(self, frame: bytes) -> tuple[list[bytes], VadEvent | None]:
        c = self.config
        speech = self.classifier.is_speech(frame)
        event: VadEvent | None = None
        forward: list[bytes] = []

        if not self.in_speech:
            if speech:
                self._speech_run += 1
                if self._speech_run * c.frame_ms >= c.start_ms:
                    self.in_speech = True
                    self._silence_run = 0
                    event = VadEvent.SPEECH_START
                    forward.extend(self._preroll)
                    self._preroll.clear()
                    forward.append(frame)
                    self._postroll_left = 0
                    return forward, event
            else:
                self._speech_run = 0
            # buffer pre-roll
            self._preroll.append(frame)
            max_frames = max(1, c.preroll_ms // c.frame_ms)
            if len(self._preroll) > max_frames:
                del self._preroll[0]
            if self._postroll_left > 0:
                self._postroll_left -= 1
                forward.append(frame)
            return forward, event

        # in speech
        forward.append(frame)
        if speech:
            self._silence_run = 0
        else:
            self._silence_run += 1
            if self._silence_run * c.frame_ms >= c.end_ms:
                self.in_speech = False
                self._speech_run = 0
                self._silence_run = 0
                self._postroll_left = max(0, c.postroll_ms // c.frame_ms)
                event = VadEvent.SPEECH_END
        return forward, event
