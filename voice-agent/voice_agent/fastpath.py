"""Deterministic replies for utterances that do not need a language model.

Every LLM call costs input tokens for the whole conversation plus output
tokens plus 300-600 ms of latency. A handful of intents cover a surprising
share of turns in task-oriented calls (acknowledgements, "say that again",
"hold on", goodbyes). They are matched here first; the reply text is fixed, so
its TTS audio is a guaranteed cache hit as well.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FastPathResult:
    intent: str
    reply: tuple[str, ...]  # sentences; each is a separate (cacheable) TTS request
    action: str | None = None  # "hangup" | "repeat" | None


@dataclass
class FastPath:
    goodbye_reply: str = "Thanks for calling. Goodbye!"
    hold_reply: str = "Sure, take your time."
    thanks_reply: str = "You're welcome. Anything else?"
    repeat_intro: str = "Sure."
    silence_reply: str = "Are you still there?"
    enabled: bool = True

    _RULES = (
        (
            "goodbye",
            re.compile(
                r"\b(good\s*bye|bye)\b|^\W*(no[, ]*)?(that'?s|that is|that'?ll be)\s+(all|it|everything)\W*$|\bnothing else\b",
                re.I,
            ),
        ),
        (
            "repeat",
            re.compile(
                r"\b(say (that|it) again|repeat (that|it)|come again|pardon|what was that|didn'?t catch)\b",
                re.I,
            ),
        ),
        (
            "hold",
            re.compile(
                r"\b(hold on|one (second|sec|moment)|just a (second|sec|moment|minute)|hang on)\b", re.I
            ),
        ),
        ("thanks", re.compile(r"^\W*(thanks|thank you|thanks a lot|cheers)\W*$", re.I)),
    )

    def match(self, text: str, last_agent_sentences: list[str] | None) -> FastPathResult | None:
        if not self.enabled:
            return None
        cleaned = text.strip()
        if not cleaned:
            return None
        for intent, pattern in self._RULES:
            if pattern.search(cleaned):
                if intent == "goodbye":
                    return FastPathResult(intent, (self.goodbye_reply,), "hangup")
                if intent == "repeat":
                    if not last_agent_sentences:
                        return None
                    # re-speaking the exact sentences makes every one a TTS cache hit
                    return FastPathResult(intent, (self.repeat_intro, *last_agent_sentences), "repeat")
                if intent == "hold":
                    return FastPathResult(intent, (self.hold_reply,))
                if intent == "thanks":
                    return FastPathResult(intent, (self.thanks_reply,))
        return None
