import json

from voice_agent.audio import MULAW_8K, PCM16_8K, PCM16_16K
from voice_agent.providers.deepgram import DeepgramSTT, DeepgramTTS, parse_listen_message


def test_parse_listen_message():
    msg = {
        "type": "Results",
        "is_final": True,
        "channel": {"alternatives": [{"transcript": " hello there ", "confidence": 0.9}]},
    }
    t = parse_listen_message(json.dumps(msg))
    assert t.text == "hello there" and t.is_final and t.confidence == 0.9
    msg["is_final"] = False
    assert parse_listen_message(json.dumps(msg)).is_final is False
    assert parse_listen_message(json.dumps({"type": "Metadata"})) is None
    msg["channel"]["alternatives"][0]["transcript"] = ""
    assert parse_listen_message(json.dumps(msg)) is None


def test_params_follow_transport_format():
    stt = DeepgramSTT(api_key="k")
    assert stt.sku == "deepgram/nova-3"
    p = stt.listen_params(PCM16_8K)
    assert p["encoding"] == "linear16" and p["sample_rate"] == "8000" and p["interim_results"] == "true"
    assert DeepgramSTT(api_key="k").listen_params(MULAW_8K)["encoding"] == "mulaw"
    tts = DeepgramTTS(api_key="k")
    assert tts.sku == "deepgram/aura-2"
    q = tts.speak_params(PCM16_16K)
    assert q == {
        "model": "aura-2-thalia-en",
        "encoding": "linear16",
        "sample_rate": "16000",
        "container": "none",
    }
