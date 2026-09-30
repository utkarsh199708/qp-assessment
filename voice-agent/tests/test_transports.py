import base64
import json

from voice_agent.audio import PCM16_8K, PCM16_16K, pcm16_to_mulaw, tone
from voice_agent.transports.twilio import TwilioMediaStreamTransport, clear_message, media_message
from voice_agent.transports.websocket import WebSocketPCMTransport


class FakeWS:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)

    async def send(self, msg):
        self.sent.append(msg)

    async def close(self):
        self.closed = True


async def test_twilio_inbound_media_becomes_pcm_frames():
    mu = pcm16_to_mulaw(tone(PCM16_8K, 0.1))  # 800 bytes = 5 x 20 ms
    msgs = [
        json.dumps({"event": "connected"}),
        json.dumps({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1"}}),
    ]
    for i in range(0, len(mu), 160):
        msgs.append(
            json.dumps({"event": "media", "media": {"payload": base64.b64encode(mu[i : i + 160]).decode()}})
        )
    msgs.append(json.dumps({"event": "stop"}))
    msgs.append(json.dumps({"event": "media", "media": {"payload": "AAAA"}}))  # after stop: never read
    ws = FakeWS(msgs)
    t = TwilioMediaStreamTransport(ws)
    frames = [f async for f in t.frames()]
    assert t.stream_sid == "MZ1" and t.call_sid == "CA1"
    assert len(frames) == 5 and all(len(f) == 320 for f in frames)
    assert ws.incoming  # the post-stop message was not consumed


async def test_twilio_outbound_messages():
    ws = FakeWS([])
    t = TwilioMediaStreamTransport(ws)
    await t.send_audio(b"\x00" * 320)  # no streamSid yet: dropped
    assert ws.sent == []
    t.stream_sid = "MZ1"
    pcm = tone(PCM16_8K, 0.02)
    await t.send_audio(pcm)
    msg = json.loads(ws.sent[-1])
    assert msg["event"] == "media" and msg["streamSid"] == "MZ1"
    assert base64.b64decode(msg["media"]["payload"]) == pcm16_to_mulaw(pcm)
    assert ws.sent[-1] == media_message("MZ1", pcm)
    await t.clear()
    assert ws.sent[-1] == clear_message("MZ1")
    await t.hangup()
    assert ws.closed and t.closed
    await t.send_audio(pcm)  # after hangup nothing is sent
    assert len(ws.sent) == 2


async def test_websocket_pcm_transport():
    audio = tone(PCM16_16K, 0.05)  # 1600 bytes = 2.5 frames
    ws = FakeWS(
        [
            audio[:1000],
            audio[1000:],
            json.dumps({"type": "noise"}),
            json.dumps({"type": "hangup"}),
            b"\x00" * 640,
        ]
    )
    t = WebSocketPCMTransport(ws)
    frames = [f async for f in t.frames()]
    assert len(frames) == 2 and all(len(f) == 640 for f in frames)
    await t.send_audio(b"\x01\x02")
    await t.clear()
    await t.hangup()
    assert ws.sent[0] == b"\x01\x02"
    assert json.loads(ws.sent[1]) == {"type": "clear"}
    assert json.loads(ws.sent[2]) == {"type": "hangup"}
    assert ws.closed
