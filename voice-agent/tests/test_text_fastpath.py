from voice_agent.fastpath import FastPath
from voice_agent.text import SentenceChunker


def test_chunker_splits_sentences_as_they_complete():
    c = SentenceChunker()
    out = []
    for piece in ["Sure, I can ", "help. What day ", "works? Dr. Smith is ", "in on Monday.", " Bye"]:
        out += c.push(piece)
    out += [c.flush() or ""]
    assert out == ["Sure, I can help.", "What day works?", "Dr. Smith is in on Monday.", "Bye"]
    assert c.flush() is None


def test_chunker_soft_max_splits_at_comma():
    c = SentenceChunker(soft_max_chars=40)
    long = "This is a fairly long clause that keeps going, and then continues for a while more"
    out = c.push(long)
    assert out and out[0].endswith(",")
    assert c.flush() is not None


def test_fastpath_intents():
    fp = FastPath()
    last = ["You're booked for Monday.", "Anything else?"]
    assert fp.match("Thanks, that's all. Bye!", last).action == "hangup"
    assert fp.match("goodbye", last).intent == "goodbye"
    assert fp.match("no that's it", last).intent == "goodbye"
    assert fp.match("that's not all", last) is None
    r = fp.match("sorry, could you say that again?", last)
    assert r.intent == "repeat" and r.reply == ("Sure.", *last)
    assert fp.match("say that again", None) is None
    assert fp.match("hold on one sec", last).intent == "hold"
    assert fp.match("thank you", last).intent == "thanks"
    assert fp.match("I'd like to book an appointment", last) is None
    assert fp.match("   ", last) is None
    assert FastPath(enabled=False).match("bye", last) is None
