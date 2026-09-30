# voice-agent

**A low-cost, real-time voice AI pipeline with a cost meter built in.**

VAD → streaming STT → LLM → streaming TTS, over Twilio Media Streams or a plain WebSocket, with every cost lever
(STT gating, TTS caching, no-LLM fast paths, barge-in, prompt caching, short outputs) as a flag and every billable
unit (telephony seconds, STT seconds sent, LLM tokens by cache class, TTS characters, compute share) priced into a
per-call ledger. The design, the price comparisons and the reasoning are in [docs/DESIGN.md](docs/DESIGN.md).

```
 phone ──Twilio/Telnyx──▶ ┐                 ┌─ Endpointer (VAD, pre/post-roll, barge-in)
 app   ──WebSocket──────▶ ├─▶ CallSession ──├─ STT (Deepgram Nova-3 | mock)     only speech is sent
                          ┘                 ├─ FastPath | LLM (Claude Haiku 4.5 | mock)  streamed, cached prefix
                                            ├─ TTS cache → TTS (Deepgram Aura-2 | mock)  per sentence, streamed
                                            └─ CostMeter → ledgers/<call>.json
```

Headline (3-minute call, list prices as of 2026-09-30): **budget-hosted $0.030/min**, self-hosted STT/TTS $0.013/min,
fully self-hosted $0.005/min, versus $0.087/min for a speech-to-speech API and $0.089/min for a managed platform.

## Quickstart

```bash
cd voice-agent
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 1. price a call under every profile (no keys, no network)
.venv/bin/voice-agent estimate --minutes 3
.venv/bin/voice-agent estimate --minutes 3 --system-tokens 4500      # knowledge base in the prompt -> caching engages

# 2. run a scripted call through the real pipeline with offline providers; watch each lever change the ledger
.venv/bin/voice-agent simulate --profile budget-hosted
.venv/bin/voice-agent simulate --compare

# 3. serve calls (mock providers: no keys needed)
.venv/bin/voice-agent serve --mock --port 8765
.venv/bin/python examples/ws_client.py ws://localhost:8765/          # a fake caller; ledger lands in ledgers/

# 4. serve calls with real providers
export DEEPGRAM_API_KEY=... ANTHROPIC_API_KEY=...
.venv/bin/voice-agent serve --port 8765
# Twilio: <Connect><Stream url="wss://your-host/twilio"/></Connect>
```

Every call prints a ledger like:

```
call sim: 58.4s  total $0.0235  ($0.0241/min)
  llm        claude-haiku-4-5   input_per_mtok    763.00 tokens   $0.00076
  stt        deepgram/nova-3    stream_per_min     20.60 seconds  $0.00269   <- 21 s billed of 58 s (VAD gating)
  telephony  telnyx/voice       inbound_per_min    58.45 seconds  $0.00520
  tts        deepgram/aura-2    per_1m_chars      360.00 chars    $0.01080
  counters: barge_ins=1, fastpath_hits=3, tts_cache_hits=2, response_latency_avg_s=0.539, ...
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Claude (or `ant auth login`) |
| `DEEPGRAM_API_KEY` | – | Nova-3 STT and Aura-2 TTS |
| `VA_LLM_MODEL` | `claude-haiku-4-5` | any Claude model; thinking is configured per family for low latency |
| `VA_TTS_VOICE` | `aura-2-thalia-en` | Deepgram voice |
| `VA_STT_MODEL` | `nova-3` | Deepgram model (`flux` for native end-of-turn) |
| `VA_SYSTEM_PROMPT_FILE` / `VA_GREETING` | built-in demo | persona / first sentence |

Levers and timings are fields on `voice_agent.pipeline.SessionConfig`; prices live in `voice_agent/pricebook.json`
(each SKU carries its source, date and a `verification` tag) and can be swapped with `--pricebook path.json`.

## Project layout

```
voice_agent/
  pipeline.py      CallSession (endpointer, barge-in, fast path, LLM stream, sentence chunking, TTS cache, pacing, idle, meter)
  vad.py           energy VAD + endpointer; Silero pluggable
  costs.py         PriceBook, CostMeter, prompt-cache arithmetic       pricebook.json  list prices with sources
  estimate.py      analytical cost model (Profile x Assumptions)       profiles.py     the priced stacks
  simulate.py      scripted calls on a virtual clock, lever comparison  fastpath.py     no-LLM intents
  providers/       base protocols; mock; anthropic_llm; deepgram        transports/     sim; websocket; twilio
  server.py        WebSocket server (/twilio, /), per-call ledgers      cli.py          estimate | simulate | pricebook | serve
docs/DESIGN.md     the design: cost anatomy, architecture, levers, tiers, latency budget, operations, verification
examples/          ws_client.py (fake caller / load generator)
tests/             33 offline tests
```

## Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check voice_agent tests
```

## What is and is not verified

Verified offline: the cost arithmetic, endpointer, chunker, fast paths, full simulated calls with every lever,
the Twilio and WebSocket transports (fake sockets), the Claude adapter's request shape and billing (fake SDK
client), Deepgram message parsing. Written against vendor docs but **not run against the live services** in this
environment: the Deepgram WebSocket/REST calls and the live Claude call. Prices tagged `snippet`/`unverified` in the
price book came from search excerpts because vendor pages were unreachable when researched; see the design doc.
