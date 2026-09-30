"""Sentence chunking for streamed LLM text.

TTS is started per sentence as soon as the sentence closes, so the first audio
plays while the model is still writing the rest. Long clause-y sentences are
split at a comma once they pass ``soft_max_chars`` to bound time-to-first-audio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")
_ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|vs|e\.g|i\.e|No)\.$", re.I)


@dataclass
class SentenceChunker:
    min_chars: int = 8
    soft_max_chars: int = 140
    _buf: str = field(default="", init=False)

    def push(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while True:
            m = _BOUNDARY.search(self._buf)
            if m is None:
                break
            head = self._buf[: m.start()].strip()
            if len(head) < self.min_chars or _ABBREV.search(head):
                # too short or an abbreviation: keep accumulating past this boundary
                nxt = _BOUNDARY.search(self._buf, m.end())
                if nxt is None:
                    break
                m = nxt
                head = self._buf[: m.start()].strip()
            out.append(head)
            self._buf = self._buf[m.end() :]
        if len(self._buf) > self.soft_max_chars:
            cut = self._buf.rfind(", ")
            if cut > self.min_chars:
                out.append(self._buf[: cut + 1].strip())
                self._buf = self._buf[cut + 2 :]
        return out

    def flush(self) -> str | None:
        tail = self._buf.strip()
        self._buf = ""
        return tail or None
