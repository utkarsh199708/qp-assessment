"""Request shape and billing integration for the Claude adapter, with a fake SDK client."""

from types import SimpleNamespace

from voice_agent.clock import SimClock
from voice_agent.fastpath import FastPath
from voice_agent.pipeline import CallSession, SessionConfig
from voice_agent.providers.anthropic_llm import ClaudeLLM, build_messages, generation_params
from voice_agent.providers.base import Message
from voice_agent.providers.mock import ScriptedSTT, ToneTTS
from voice_agent.transports.sim import ScriptLine, SimTransport


class FakeStream:
    def __init__(self, params, text, usage):
        self.params = params
        self._text = text
        self._usage = usage
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    @property
    def text_stream(self):
        async def gen():
            for word in self._text.split(" "):
                yield word + " "

        return gen()

    async def get_final_message(self):
        return SimpleNamespace(stop_reason="end_turn", usage=self._usage)


class FakeMessages:
    def __init__(self):
        self.calls = []

    def stream(self, **params):
        self.calls.append(params)
        n = len(self.calls)
        usage = SimpleNamespace(
            input_tokens=40,
            cache_creation_input_tokens=4200 if n == 1 else 90,
            cache_read_input_tokens=0 if n == 1 else 4200 + 90 * (n - 2),
            output_tokens=25,
        )
        return FakeStream(params, "Sure, I can help with that. What day works for you?", usage)


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()


def test_request_shape_per_model():
    llm = ClaudeLLM("claude-haiku-4-5", client=FakeClient())
    p = llm.request_params("SYSTEM", [Message("assistant", "Hi!"), Message("user", "book please")])
    assert p["model"] == "claude-haiku-4-5" and p["max_tokens"] == 250
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert p["messages"][0] == {"role": "user", "content": "[call connected]"}
    assert p["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in p and "output_config" not in p
    assert generation_params("claude-sonnet-5-5") == {"thinking": {"type": "between_tools"}}
    assert generation_params("claude-opus-5-5") == {"output_config": {"effort": "low"}}
    llm_1h = ClaudeLLM("claude-sonnet-5-5", client=FakeClient(), cache_ttl="1h", cache_history=False)
    p = llm_1h.request_params("S", [Message("user", "hi")])
    assert p["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert p["messages"] == [{"role": "user", "content": "hi"}]
    assert build_messages([], cache_history=True) == []


async def test_pipeline_bills_claude_usage(book):
    client = FakeClient()
    llm = ClaudeLLM("claude-haiku-4-5", client=client)
    script = [ScriptLine("I want to book"), ScriptLine("Thursday"), ScriptLine("bye")]
    async with SimClock() as clock:
        transport = SimTransport(script, clock)
        session = CallSession(
            transport=transport,
            stt=ScriptedSTT([line.say for line in script], clock=clock),
            llm=llm,
            tts=ToneTTS(clock=clock),
            book=book,
            config=SessionConfig(system_prompt="S" * 100, greeting="Hello!"),
            clock=clock,
            fastpath=FastPath(),
            call_id="claude-fake",
        )
        meter = await session.run()
    assert len(client.messages.calls) == 2  # two LLM turns; "bye" is a fast path
    assert client.messages.calls[1]["messages"][0]["content"] == "[call connected]"
    lines = {line.rate: line for line in meter.lines() if line.component == "llm"}
    assert lines["cache_write_per_mtok"].quantity == 4200 + 90
    assert lines["cache_read_per_mtok"].quantity == 4200
    assert lines["input_per_mtok"].quantity == 80
    assert lines["output_per_mtok"].quantity == 50
    assert meter.counters["llm_calls"] == 2
    assert session.end_reason == "agent_hangup"
