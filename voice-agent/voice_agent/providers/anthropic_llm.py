"""Claude via the official SDK, tuned for voice: streaming, short replies, prompt caching.

Cost/latency choices baked in:
- ``max_tokens`` defaults to 250: a voice reply is two or three sentences.
- The system prompt is one cached block (``cache_control``) and the last user
  message carries a second breakpoint so the growing conversation is read from
  cache on the next turn. Reads cost 5-10 % of the input price. Claude Haiku
  4.5 only caches prefixes of 4096+ tokens, so a short system prompt is billed
  in full there; Sonnet 5.5 / Opus 5.5 cache from 512 tokens.
- Thinking is off or minimal per model family (below); voice needs the first
  token in a few hundred milliseconds, not a considered essay.
- Usage is read from the final message so the meter bills exactly what
  Anthropic billed, including cache reads/writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ..costs import CacheSplit
from .base import LLMUsage, Message

DEFAULT_MODEL = "claude-haiku-4-5"


def generation_params(model: str) -> dict[str, Any]:
    """Per-family request fields that keep thinking off/minimal for low latency.

    - Haiku 4.5: omit ``thinking`` (no thinking).
    - Sonnet 5.5: ``{"type": "between_tools"}`` is the lowest thinking setting
      (``disabled`` returns a 400 on this model).
    - Opus 5.5 / Opus 5 / Fable: thinking cannot be disabled; ``effort: low``
      keeps it short.
    """
    if "haiku" in model:
        return {}
    if "sonnet-5-5" in model:
        return {"thinking": {"type": "between_tools"}}
    if "opus-5" in model or "fable" in model or "mythos" in model:
        return {"output_config": {"effort": "low"}}
    return {}


def build_messages(history: list[Message], *, cache_history: bool) -> list[dict[str, Any]]:
    """API-shaped messages. The first message must be from the user, so a
    conversation that opened with the agent's greeting gets a synthetic
    ``[call connected]`` user turn in front."""
    msgs: list[dict[str, Any]] = []
    if history and history[0].role != "user":
        msgs.append({"role": "user", "content": "[call connected]"})
    for m in history:
        msgs.append({"role": m.role, "content": m.content})
    if cache_history:
        # breakpoint on the last user message: next turn reads everything before it from cache
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i]["role"] == "user":
                text = msgs[i]["content"]
                msgs[i] = {
                    "role": "user",
                    "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}],
                }
                break
    return msgs


class ClaudeStream:
    def __init__(self, client: anthropic.AsyncAnthropic, params: dict[str, Any]):
        self._client = client
        self._params = params
        self.usage: LLMUsage | None = None
        self.stop_reason: str | None = None
        self._stream: Any = None

    async def __aiter__(self) -> AsyncIterator[str]:
        async with self._client.messages.stream(**self._params) as stream:
            self._stream = stream
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
            self.stop_reason = final.stop_reason
            self.usage = usage_from_api(final.usage)

    async def aclose(self) -> None:
        if self.usage is None and self._stream is not None:
            # interrupted by barge-in: bill what was generated so far, best effort
            try:
                snap = self._stream.current_message_snapshot
                self.usage = usage_from_api(snap.usage)
                self.stop_reason = "cancelled"
            except Exception:
                pass


def usage_from_api(u: Any) -> LLMUsage:
    return LLMUsage(
        split=CacheSplit(
            uncached=int(getattr(u, "input_tokens", 0) or 0),
            cache_write=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            cache_read=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        ),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
    )


class ClaudeLLM:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        max_tokens: int = 250,
        cache_system: bool = True,
        cache_history: bool = True,
        cache_ttl: str | None = None,  # "1h" for bursty traffic with >5 min gaps; default 5 min
        timeout_s: float = 20.0,
    ):
        self.sku = model
        self.model = model
        self.max_tokens = max_tokens
        self.cache_system = cache_system
        self.cache_history = cache_history
        self.cache_ttl = cache_ttl
        self._client = client or anthropic.AsyncAnthropic(timeout=timeout_s, max_retries=1)

    def request_params(self, system: str, history: list[Message]) -> dict[str, Any]:
        cache_control: dict[str, Any] = {"type": "ephemeral"}
        if self.cache_ttl:
            cache_control["ttl"] = self.cache_ttl
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": build_messages(history, cache_history=self.cache_history),
        }
        if self.cache_system:
            params["system"] = [{"type": "text", "text": system, "cache_control": cache_control}]
        else:
            params["system"] = system
        params.update(generation_params(self.model))
        return params

    def stream(self, system: str, history: list[Message]) -> ClaudeStream:
        return ClaudeStream(self._client, self.request_params(system, history))
