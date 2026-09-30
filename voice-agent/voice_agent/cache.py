"""TTS audio cache: the same sentence in the same voice is bought once.

Greetings, confirmations, error prompts and fast-path replies repeat across
calls; caching them removes their TTS cost entirely and also removes the TTS
round-trip from the latency budget. Keyed on (sku, voice, format, normalised text).
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict

from .audio import AudioFormat


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def cache_key(sku: str, voice: str, fmt: AudioFormat, text: str) -> str:
    raw = f"{sku}|{voice}|{fmt.sample_rate}|{fmt.encoding}|{normalise(text)}"
    return hashlib.sha256(raw.encode()).hexdigest()


class TTSCache:
    def __init__(self, max_items: int = 2000, max_bytes: int = 64 * 1024 * 1024):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> bytes | None:
        audio = self._items.get(key)
        if audio is None:
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return audio

    def put(self, key: str, audio: bytes) -> None:
        if not audio:
            return
        if key in self._items:
            self._bytes -= len(self._items[key])
        self._items[key] = audio
        self._items.move_to_end(key)
        self._bytes += len(audio)
        while self._items and (len(self._items) > self.max_items or self._bytes > self.max_bytes):
            _, evicted = self._items.popitem(last=False)
            self._bytes -= len(evicted)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def size_bytes(self) -> int:
        return self._bytes
