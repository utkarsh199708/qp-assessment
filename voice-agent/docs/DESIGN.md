# Low-cost voice AI: design

*Status: v1 design with a working reference implementation (`voice_agent/`). Prices are list prices as of 2026-09-30; see [Price verification](#price-verification) before quoting them.*

## 0. Summary

A real-time voice agent is a **cascade**: voice activity detection (VAD) → streaming speech-to-text (STT) → a language model (LLM) → streaming text-to-speech (TTS), glued to a phone line or a browser. The cascade is the cheapest architecture because each stage can be bought from the cheapest adequate vendor, gated so it is only paid for while it is actually needed, or replaced with a self-hosted open model. Speech-to-speech models and managed platforms are simpler to start with and 3-10x more expensive per minute.

Headline numbers from the estimator (3-minute inbound call, 4 user turns per minute, list prices, US):

| Profile | $/min | $/call | 100k min/month | What it is |
|---|---|---|---|---|
| **quality-value** (recommended for production) | **$0.027** | **$0.080** | $2,661 | Telnyx PSTN, Deepgram Nova-3, Claude Sonnet 5.5 (thinking off, cached prompt), Deepgram Aura-2, one small VM |
| budget-hosted | $0.030 | $0.089 | $2,950 | same with Claude Haiku 4.5; cheaper than Sonnet only once the prompt exceeds Haiku's 4,096-token cache minimum |
| budget-hosted with a 4.5k-token knowledge prompt, cached | $0.026 | $0.078 | $2,607 | Haiku with prompt caching engaged (Sonnet: $0.093) |
| floor-hosted | $0.014 | $0.041 | $1,362 | AssemblyAI streaming, Qwen3-14B on DeepInfra, Inworld TTS Mini: cheapest parts, weakest instruction-following |
| self-hosted | $0.013 | $0.039 | $1,284 | SIP trunk, Parakeet STT + Kokoro TTS on one shared L4, Claude Haiku 4.5 |
| self-hosted-llm | $0.005 | $0.015 | $495 | above plus Qwen3-8B on a second L4; lowest marginal, highest fixed/ops cost |
| balanced-hosted | $0.033 | $0.099 | $3,295 | Twilio, Nova-3, Claude Sonnet 5.5, Cartesia Sonic |
| premium-hosted | $0.037 | $0.110 | $3,659 | Twilio, Nova-3, Claude Sonnet 5.5, ElevenLabs Flash |
| speech-to-speech | $0.087 | $0.261 | $8,702 | Twilio + OpenAI Realtime (gpt-realtime) |
| managed-platform | $0.089 | $0.267 | $8,890 | Twilio + Retell all-in |

Reproduce with `voice-agent estimate --minutes 3` and `voice-agent estimate --minutes 3 --system-tokens 4500`.

The three decisions that matter most, in order of dollars saved:

1. **Cascade, not speech-to-speech or a platform.** Saves 60-70 % of the bill by itself.
2. **Only pay for what is needed:** gate STT with VAD, cache TTS audio, answer trivial turns without the LLM, cut TTS on barge-in, keep LLM prompts cached and replies short. Together these cut the cascade's variable cost by 30-50 % (measured in the simulator, section 4).
3. **Model choice by cache behaviour, not list price.** Claude Sonnet 5.5 ($2/$10 per MTok) caches prompts from 512 tokens; Haiku 4.5 ($1/$5) only from 4,096. With a typical 1-3k-token persona prompt Sonnet's cached turns are *cheaper* than Haiku's uncached ones ($0.017 vs $0.025 per call) and resolve harder calls. Haiku wins only when the prompt carries a 4k+-token knowledge base ($0.015 vs $0.030).

Everything else (self-hosting STT/TTS, self-hosting the LLM, negotiating telephony) is a volume game: worth it above roughly 200k-500k minutes per month, not before.

## 1. Goal, scope, assumptions

**Goal.** Serve inbound and outbound task-oriented calls (bookings, order status, FAQ, triage, reminders) at the lowest cost per *resolved* call while keeping voice-to-voice latency under about one second so the conversation feels natural.

**Scope.** English first; PSTN callers (Twilio/Telnyx media streams or a SIP trunk) and app/web callers (WebSocket or WebRTC). One agent persona per deployment, tools optional. No human agent handoff in v1 (a `transfer` fast-path reply is the placeholder).

**Assumptions used in the cost model** (all adjustable in `voice_agent.estimate.Assumptions`):

| Assumption | Value | Basis |
|---|---|---|
| Call length | 3 min | typical task-oriented call |
| User turns | 4 per minute | ~12 per call |
| Caller speaking | 40 % of the call | rest is agent speech and silence |
| Agent speaking | 40 % of the call | ~850 characters per spoken minute (~150 wpm) |
| System prompt | 1,500 tokens (4,500 with a knowledge base) | persona + rules (+ FAQ) |
| User / agent turn | 35 / 60 tokens | short spoken sentences |
| TTS cache hit rate | 15 % | greeting, confirmations, fast-path replies |
| Fast-path share of turns | 10 % | acknowledgements, repeats, goodbyes |
| Self-hosted capacity utilisation | 35 % | hourly-billed GPUs sit idle off-peak |

## 2. Where a voice minute's money goes

For the budget-hosted profile a 3-minute call costs about 8.9 cents:

| Component | $/call | Share | Driver |
|---|---|---|---|
| Telephony (Telnyx PSTN + media streaming) | 0.026 | 30 % | wall-clock minutes, whole-minute increments |
| TTS (Deepgram Aura-2, $30/1M chars) | 0.026 | 29 % | characters the agent speaks |
| LLM (Claude Haiku 4.5) | 0.025 | 29 % | input tokens re-sent every turn: system prompt + growing history |
| STT (Deepgram Nova-3, $0.0077/min) | 0.010 | 12 % | audio *sent* to the provider |
| Compute (shared VM) | 0.001 | <1 % | negligible unless you self-host models |

Three things fall out of this table:

- **There is no single dominant cost.** Telephony, TTS and LLM are each about 30 %. A design that only optimises the LLM leaves two thirds of the bill untouched.
- **The LLM bill is an input-token bill.** Output is 12 × 60 = 720 tokens (0.4 cents). Input is 22,000 tokens (2.2 cents) because the whole conversation is re-sent on each turn. Prompt caching and short histories are the levers, not a cheaper output price.
- **Compute is not where the money is** unless you move models in-house. A 4-vCPU box runs dozens of pipeline sessions; VAD is microseconds per frame.

Speech-to-speech models invert this: audio tokens are billed for every second of inbound audio (silence included) and re-billed as the context grows, which is why the measured cost of OpenAI Realtime deployments is reported at $0.06-0.11/min rather than the naive $0.02-0.03.

## 3. Architecture

### 3.1 Why a cascade

| Option | $/min (list) | Latency | Control | Verdict |
|---|---|---|---|---|
| Managed platform (Retell, Vapi, Bland, Synthflow) | 0.09-0.15 all-in | 600-900 ms | low: their models, their prompts, their bugs | fastest to launch; 3x the cascade's cost; fine for a pilot |
| Speech-to-speech (OpenAI Realtime, Gemini Live, Nova Sonic) | 0.03-0.11 | 300-600 ms | medium: one model does everything, hard to inspect or swap | best prosody and interruption handling; expensive at scale, weak tool discipline, context re-billing |
| **Cascade (this design)** | **0.013-0.037** | 500-900 ms | high: every stage swappable, gated and metered | chosen |

The cascade's extra 200-300 ms of latency versus speech-to-speech is real. It is recovered by streaming every stage (sentence-level TTS while the LLM is still writing) and by the fast paths, and it is the price of a bill that is a third of the size.

### 3.2 Components and data flow

```
 caller ──PSTN──▶ Twilio/Telnyx Media Streams ──WS (mu-law 8k)──▶ ┐
 caller ──SIP───▶ LiveKit SIP / Asterisk ───────────────────────▶ ├─▶ Transport (decode to PCM16 frames)
 app    ──WS/WebRTC (PCM16 16k) ───────────────────────────────▶ ┘            │
                                                                              ▼ 20 ms frames
                                            ┌──────────────── CallSession (one per call, one process per N calls) ───────────────┐
                                            │  Endpointer (VAD + pre/post-roll)                                                 │
                                            │    ├─ SPEECH_START ──▶ barge-in: cancel response, transport.clear()               │
                                            │    ├─ speech frames ──▶ STT session (only these are billed)                       │
                                            │    └─ SPEECH_END ────▶ stt.finalize() ──▶ final transcript ──▶ user turn          │
                                            │                                                                                    │
                                            │  user turn ──▶ FastPath? ──yes──▶ canned sentences ──────────────┐                 │
                                            │                  └──no──▶ LLM stream ──▶ sentence chunker ──▶ sentences            │
                                            │                                                                  ▼                 │
                                            │                                   TTS cache ──miss──▶ TTS provider (streaming)     │
                                            │                                        └──hit───▶ audio bytes                      │
                                            │                                                       ▼                            │
                                            │                                   paced sender (≤300 ms ahead of real time)        │
                                            │  CostMeter: telephony s, STT s sent, LLM tokens (uncached/cache-write/cache-read/  │
                                            │             output), TTS chars on miss, compute share  ──▶ per-call ledger JSON    │
                                            └────────────────────────────────────────────────────────────────────────────────────┘
```

Everything is asynchronous inside one Python process; a session is four coroutines (inbound audio, STT events, dispatcher, idle watchdog) plus one cancellable response task at a time. Sessions share nothing but the TTS cache and the provider clients, so a process hosts many calls and instances scale horizontally behind a WebSocket load balancer.

### 3.3 Turn-taking

The endpointer (`voice_agent/vad.py`) is the one component that touches three cost levers, so it is owned by the pipeline rather than delegated to the STT vendor:

```
            speech ≥ 60 ms                          silence ≥ 500 ms
 IDLE ───────────────────────▶ IN_SPEECH ───────────────────────────▶ IDLE (+200 ms post-roll to STT)
  │  keeps a 240 ms pre-roll        │ forwards every frame to STT          │ stt.finalize() → transcript → LLM
  │  ring buffer (sent on start)    │ if agent is speaking: BARGE-IN       │ endpoint timestamp = latency clock start
```

- **Start threshold** rejects clicks and breaths. **Pre-roll** is sent so the first phoneme is not clipped. **Post-roll** covers a trailing consonant.
- **End threshold** is the biggest latency knob. 500 ms is a safe default for phone speech; Deepgram Flux or a semantic turn detector (Pipecat Smart Turn, LiveKit turn detector) can cut it to 200-300 ms with fewer false cut-offs, at the cost of a model.
- **Barge-in** cancels the response task, clears the transport's playback buffer, and records the partially spoken sentence in history with an `[interrupted]` marker so the model knows what the caller actually heard. Because audio is pushed at most 300 ms ahead of real time, `clear()` has little to drop and the interruption feels immediate.
- **Echo.** On PSTN the carrier cancels echo; in browsers WebRTC does. Over a plain WebSocket the client must run acoustic echo cancellation or the agent will interrupt itself. The energy VAD is deliberately conservative during agent speech for this reason; Silero VAD is a drop-in `FrameClassifier` when the environment is noisy.

### 3.4 Provider choices per stage

| Stage | Budget choice | Why | Alternatives (when) |
|---|---|---|---|
| Telephony | Telnyx Voice API + media streaming ($0.0087/min in) or Plivo (6 s increments) | cheapest streaming access to PSTN audio; whole-minute vs 6 s increments matter on short calls | Twilio ($0.0129/min in) for ecosystem; SIP trunk ($0.0032/min) once you run your own media stack; **WebRTC/WebSocket for app users costs nothing per minute** |
| STT | Deepgram Nova-3 streaming | bills audio sent (so VAD gating works), `Finalize` and `KeepAlive` control messages, <300 ms, mu-law 8 k input | AssemblyAI ($0.0025/min) is 3x cheaper on paper but bills session time; self-hosted Parakeet/faster-whisper at volume |
| LLM | Claude Haiku 4.5 | $1/$5 per MTok, low TTFT, strong instruction following and tool use for task-oriented dialogue | Sonnet 5.5 ($2/$10, `thinking: between_tools`) for harder tasks; hosted open-weight (Qwen3, Llama) at $0.02-0.30/MTok where quality allows; self-hosted vLLM at volume |
| TTS | Deepgram Aura-2 ($30/1M chars) | streams 8 k mu-law directly, <200 ms TTFB, half the price of ElevenLabs Flash | Inworld TTS-1.5 Mini ($5/1M) for the floor; Cartesia/ElevenLabs for brand voices; Kokoro-82M (Apache-2.0) self-hosted |
| VAD | energy VAD (built-in), Silero VAD (MIT) | free, CPU-only | Smart Turn v3 for semantic endpointing |
| Orchestration | this package, or Pipecat (BSD-2) / LiveKit Agents (Apache-2.0) | Pipecat ships Twilio/Telnyx/Plivo serialisers and every provider above | LiveKit when you need SIP-native WebRTC at scale |

### 3.5 Transports

- **Twilio Media Streams** (`transports/twilio.py`): `<Connect><Stream url="wss://host/twilio"/></Connect>`; 20 ms mu-law frames each way; `clear` is the barge-in primitive. STT and TTS are configured for mu-law/8 kHz so the pipeline never resamples.
- **Generic WebSocket** (`transports/websocket.py`): binary PCM16 in/out plus `{"type":"clear"|"hangup"}`. Any app can speak it; it is also the load-test interface.
- **SIP** (design only): LiveKit SIP or Asterisk bridging a Telnyx/Twilio trunk into WebRTC/RTP removes the carrier's media-streaming surcharge and cuts telephony by 40-60 %, at the price of running a media server.

## 4. The cost levers

Each lever is a flag on `SessionConfig`; the simulator switches them on one at a time over the same scripted 8-turn booking call (`voice-agent simulate --compare`):

| Levers | Call | Total | STT billed | LLM calls | LLM input tok | Cached tok | LLM $ | TTS chars | Latency |
|---|---|---|---|---|---|---|---|---|---|
| all off | 94 s | $0.0478 | 94 s | 8 | 1,335 | 0 | $0.0020 | 539 | 1.07 s |
| + VAD gating of STT | 94 s | $0.0383 | 21 s | 8 | 1,335 | 0 | $0.0020 | 539 | 1.07 s |
| + fast-path intents | 58 s | $0.0280 | 21 s | 5 | 763 | 0 | $0.0012 | 513 | 0.90 s |
| + TTS cache | 58 s | $0.0258 | 21 s | 5 | 763 | 0 | $0.0012 | 438 | 0.90 s |
| + barge-in | 58 s | $0.0235 | 21 s | 5 | 763 | 0 | $0.0012 | 360 | 0.54 s |
| + prompt caching (70-token prompt) | 58 s | $0.0235 | 21 s | 5 | 763 | 0 | $0.0012 | 360 | 0.54 s |
| 4.5k-token FAQ prompt, no cache | 58 s | $0.0460 | 21 s | 5 | 23,263 | 0 | $0.0237 | 360 | 0.54 s |
| 4.5k-token FAQ prompt, cached | 58 s | $0.0305 | 21 s | 5 | 23,263 | 18,537 | $0.0082 | 360 | 0.54 s |

Half the cost of the call disappears between the first and fifth rows. Note that per-*minute* cost can rise while per-*call* cost falls (the fast-path row): the goodbye intent ends the call 36 seconds sooner. Optimise cost per resolved call, never cost per minute.

**L1. VAD gating of STT.** Only speech segments (plus 240 ms pre-roll and 200 ms post-roll) are sent to the STT socket; silence and the agent's own speaking time are never uploaded. Callers speak about 40 % of a call, so the STT bill drops 55-65 %. Requires a provider that bills audio, not session time (Deepgram, Google, Azure: yes; AssemblyAI streaming: no), and `KeepAlive` messages so the socket survives quiet stretches.

**L2. Fast-path intents.** Acknowledgements, "say that again", "hold on", thanks and goodbyes are matched by regex before the LLM. Each avoided call saves the full re-sent context (1-5k input tokens) and 300-600 ms. The replies are fixed strings, so they are permanent TTS cache hits. Extend with DTMF handling and a `transfer` intent; keep the list short and unambiguous, and let the LLM own everything else (an `end_call` tool covers the ambiguous goodbyes).

**L3. TTS cache.** Audio is keyed by (provider, voice, format, normalised text). Greetings, confirmations, error prompts, fast-path replies and any sentence the model repeats verbatim are synthesised once per process (or once ever, with a shared object store). Hit rates of 15-30 % are typical for scripted flows; pre-warm the greeting and the fast-path phrases at deploy time so the first call is not slower.

**L4. Barge-in.** Cancelling the response the moment the caller speaks stops buying TTS for sentences nobody hears (33 % fewer TTS characters on the scripted call) and stops the LLM stream. Low playback lead (300 ms) is what keeps the cost and the interruption latency down.

**L5. Prompt caching.** The system prompt and the conversation prefix carry `cache_control` breakpoints; cache reads cost 10 % (Haiku) or 5-10 % (Sonnet 5.5 / Opus 5.5) of the input price. Two traps: (a) **Claude Haiku 4.5 only caches prefixes of 4,096+ tokens**, so a lean 1,500-token persona prompt is billed in full; the estimator flags this. The cure is to put the useful knowledge (FAQ, policies, menu) *in* the system prompt so the prefix crosses the minimum, which also improves answers; Sonnet 5.5 caches from 512 tokens. (b) Anything volatile in the prefix (timestamps, call IDs) invalidates it; put per-call facts in the first user turn or a mid-conversation system message. With a 4.5k-token knowledge prompt caching cuts the LLM bill by 65 %.

**L6. Short outputs, spoken style.** The prompt asks for at most two sentences and no lists or URLs; `max_tokens` is 250. Output tokens are the expensive ones ($5/MTok on Haiku) and every extra sentence is also 60 ms of TTS and a longer call. Thinking is off (`between_tools` on Sonnet 5.5, `effort: low` on Opus) because a voice turn cannot wait for it.

**L7. History trimming in blocks.** Old turns are dropped six at a time rather than one per turn, so the cached prefix stays valid for several turns between trims. For long calls, summarise instead of trimming (one cheap call replaces N re-sent turns).

**L8. Model routing.** Haiku by default; route to Sonnet 5.5 only for calls flagged as complex (tool-heavy, multilingual, negotiation), and only for the turns that need it. Judge by cost per *resolved* call: a cheaper model that needs two extra turns is not cheaper.

**L9. Telephony.** Prefer per-second or 6-second increments for short calls; prefer carriers whose media-streaming surcharge is low (Telnyx $0.0035 vs Twilio $0.0044); move app users to WebRTC/WebSocket where the transport is free; move to a SIP trunk once volume justifies a media server. Number rental is negligible.

**L10. Call-length hygiene.** Idle re-prompt at 8 s and hang-up at 25 s of silence; hard cap on call duration; the goodbye fast path; a greeting that states what the agent can do so callers get to the point. Telephony and STT are billed by the minute either way.

**L11. Compute sharing.** One 4-vCPU box hosts 40+ sessions (VAD is microseconds per frame; the rest is I/O). Autoscale on concurrent calls, not CPU. Per-call compute is well under a tenth of a cent.

**L12. Self-hosting.** STT (Parakeet-TDT 0.6B, CC-BY-4.0; faster-whisper, MIT) and TTS (Kokoro-82M, Apache-2.0) fit together on one L4-class GPU (~$0.50-0.80/hour) serving ~20 concurrent calls, and remove $0.036/min of vendor cost per call minute. At 35 % utilisation the GPU adds ~$0.001/min. The catch is fixed cost: engineering, on-call, GPU capacity for peak. `break_even_minutes()` puts the crossover against the budget-hosted profile at roughly 120k minutes/month per $3,000/month of ops cost. Self-hosting the LLM (Qwen3-8B on vLLM, ~0.5 s TTFT at 64 concurrent on an A10) needs a second GPU and drops the LLM line to compute only, but Claude Haiku's instruction-following is the reason task success rates hold; treat it as a phase-3 option.

## 5. Latency budget

Cost cuts must not make the agent slow. Target: 800 ms median from the caller's last word to the agent's first audio, 1.2 s p95.

| Stage | Budget | How the design meets it |
|---|---|---|
| Endpoint detection | 300-500 ms | trailing-silence threshold; semantic turn detector to shorten |
| STT finalisation | 100-300 ms | `Finalize` at the endpoint instead of waiting for the vendor timer |
| LLM time-to-first-token | 300-500 ms | Haiku, no thinking, cached prefix, short prompt; fast path is 0 ms |
| First sentence to TTS | 0 ms extra | sentence chunker fires at the first sentence boundary |
| TTS time-to-first-byte | 100-200 ms | Aura-2/Cartesia streaming; cache hits are 0 ms |
| Transport + carrier | 100-200 ms | keep media servers in-region; Twilio adds ~100 ms |
| **Total** | **~900 ms** | simulator with realistic vendor latencies reports 540 ms; real deployments should expect 700-900 ms |

The simulator's `response_latency_avg_s` counter measures endpoint-to-first-audio per call with the mocks' configured latencies (STT 150 ms, LLM TTFT 350 ms, TTS TTFB 120 ms). Measure the same counter in production from the ledgers.

## 6. Tiers and the recommended path

1. **Pilot and production (weeks 1-4): quality-value.** Telnyx (or Twilio if already in use), Deepgram Nova-3 + Aura-2, Claude Sonnet 5.5 with thinking off and the prompt cached, one small VM. ~$0.027/min, $0.08 per 3-minute call. All levers on from day one. Switch the LLM to Haiku 4.5 only for flows whose system prompt carries a 4k+-token knowledge base (then Haiku's cache engages and it is ~15 % cheaper); keep Sonnet where task success matters, because an unresolved call costs its full price plus a human handoff.
2. **Scale (months 2-6): tune, don't rebuild.** Watch three ledger ratios: STT-seconds-billed / call-seconds (target < 0.5), cache-read tokens / input tokens (target > 0.7), TTS cache hit rate (target > 0.2). Add Silero or Smart Turn to cut endpoint latency; add model routing to Sonnet 5.5 where task success needs it; negotiate volume pricing (Deepgram Growth, Telnyx, ElevenLabs) which typically takes 10-30 % off.
3. **Volume (> ~200k min/month): self-host STT and TTS**, keep the LLM hosted. ~$0.013/min. Requires a GPU fleet with headroom for peak and an on-call rotation.
4. **Very high volume or data residency: self-host the LLM too.** ~$0.005/min marginal. Only when a measured eval shows an open-weight model meets the task success bar.

## 7. Scaling and operations

- **Stateless sessions.** A call's state lives in one `CallSession`; nothing is shared across processes except the TTS cache (make it a shared object store for a fleet) and provider connection pools. Any instance can take any call; drain on deploy by refusing new WebSocket connections and letting calls finish.
- **Capacity.** Plan ~40 sessions per 4 vCPU (energy VAD) or ~20 (Silero + Python framework overhead, the community figure for Pipecat). Provider concurrency limits matter more than CPU: check Deepgram/Anthropic rate limits per key and shard keys.
- **Observability.** Every call writes a ledger (`ledgers/<call>.json`): dollars per component, counters for every lever, per-turn source and latency, end reason. Dashboards: $/call and $/resolved call, p50/p95 endpoint-to-first-audio, barge-ins per call, fast-path share, cache-read ratio, STT-billed ratio, end-reason mix. Alert when $/call drifts 20 % above baseline; that is how a broken cache or a vendor billing change shows up first.
- **Cost controls.** Per-call hard cap on LLM tokens and duration; per-day budget per deployment; kill switch that routes new calls to a static IVR message.

## 8. Failure modes

| Failure | Behaviour |
|---|---|
| STT socket drops mid-call | reconnect with the pre-roll buffer; if it fails twice, play a cached "I'm having trouble hearing you" and end |
| LLM timeout / 5xx / rate limit | one retry with backoff; then a cached fallback sentence; the SDK retries connection errors and 429s itself |
| LLM `stop_reason: refusal` | speak `refusal_reply` (cached) and continue; log the category |
| TTS failure | fall back to the cached phrase closest to the intent (e.g. "one moment") and retry the sentence; if the provider is down, switch to the secondary TTS |
| Barge-in false positive (noise, echo) | VAD start threshold + 60 ms minimum; on WebSocket clients require AEC; log barge-ins per minute and raise the threshold when it spikes |
| Caller silent | re-prompt at 8 s, hang up at 25 s |
| Runaway call | `max_call_s` hard stop with a polite goodbye |
| Provider price or behaviour change | the price book is data with `as_of` and `verification`; ledgers make a change visible within a day |

## 9. Security and privacy

- **Consent and recording.** Announce recording where required; do not store audio by default. The ledger stores text turns; make that opt-in for regulated deployments.
- **PII.** Redact card numbers, national IDs and similar from transcripts before they reach the LLM and the ledger (a regex pass on final transcripts is cheap); prefer tools that look data up by reference over reading it aloud.
- **Data retention.** Vendor STT/TTS data policies differ; Anthropic offers zero-data-retention arrangements on eligible models. Keep provider keys out of the repo (`DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY` via environment or a secret store).
- **Prompt injection by voice.** Caller speech is untrusted input. Tools must be authorised by call metadata (the verified caller ID, an authenticated session), never by what the caller says; the system prompt states this and the tool layer enforces it.
- **Transport security.** TLS on every WebSocket; validate Twilio's signature on the TwiML webhook; authenticate app clients with short-lived tokens.

## 10. Reference implementation

```
voice_agent/
  pipeline.py        CallSession: endpointer, barge-in, fast path, LLM stream, sentence chunking, TTS cache, paced playback, idle watchdog, meter
  vad.py             EnergyVAD + Endpointer (pre/post-roll, start/end thresholds); Silero pluggable
  costs.py           PriceBook (pricebook.json), CostMeter (per-call ledger), prompt-cache arithmetic
  estimate.py        analytical cost model: Profile x Assumptions -> ledger; monthly and break-even helpers
  profiles.py        the eight priced stacks
  simulate.py        scripted end-to-end calls on a virtual clock; lever comparison
  fastpath.py        no-LLM intents (goodbye, repeat, hold, thanks)
  cache.py           TTS audio LRU cache
  text.py            sentence chunker for streamed LLM text
  audio.py           PCM helpers, mu-law codec, resampler (pure Python)
  clock.py           real and discrete-event simulated clocks
  providers/         base protocols; mock (offline); anthropic_llm (Claude, cached, streaming); deepgram (Nova-3 WS, Aura-2 REST)
  transports/        base protocol; sim (scripted caller); websocket (PCM16); twilio (Media Streams)
  server.py          WebSocket server: /twilio and generic routes, per-call ledgers, shared TTS cache
  cli.py             estimate | simulate [--compare] | pricebook | serve
```

What is verified by the test-suite (33 tests, offline): price-book arithmetic and completeness, prompt-cache splitting, the endpointer's segmentation and gating counts, the chunker, fast paths, full simulated calls (levers, barge-in, cache sharing, idle handling, history trimming), the Twilio and WebSocket transports against fake sockets, the Claude adapter's request shape and billing integration against a fake SDK client, and Deepgram message parsing.

What is written against vendor documentation but **not exercised** here: the live Deepgram WebSocket/REST calls and the live Anthropic call (no keys in the build environment). Run `voice-agent serve --mock` to exercise the whole server path with offline providers.

## 11. Roadmap and open questions

- Semantic turn detection (Smart Turn v3 / Deepgram Flux) to cut 200-300 ms of endpoint latency; measure false cut-off rate.
- LLM tools: `end_call`, `transfer`, `lookup_booking`, with strict schemas; an eval set of scripted calls scoring task success, not just cost.
- Secondary providers with automatic failover (AssemblyAI, Cartesia) and per-provider circuit breakers.
- Shared TTS cache in object storage with pre-warming from the prompt's canned phrases.
- Summarise-instead-of-trim for long calls; per-call and per-day budget enforcement.
- Multilingual: Nova-3 multilingual ($0.0058-0.0078/min), Kokoro's 9 languages, Haiku's multilingual quality per language.
- Verify the snippet-sourced prices against vendor pages and add a monthly price-book refresh job.

## Price verification

The price book (`voice_agent/pricebook.json`) records, per SKU, the list price, unit, billing increment, source URL, the date it was checked and a `verification` tag. On 2026-09-30 the research sandbox could fetch Google Cloud's pricing pages and public GitHub sources (including LiteLLM's maintained price table), but vendor pages for Twilio, Telnyx, Deepgram, ElevenLabs, Cartesia, OpenAI, AWS, Hetzner, RunPod and the managed platforms were blocked; those rows are tagged `snippet` (the number appeared in a search-result excerpt citing the vendor page) or `unverified` (conflicting figures). Known conflicts: Deepgram Nova-3 streaming ($0.0077 list vs $0.0048 in LiteLLM; the book uses the higher figure), Azure TTS ($16 vs $15 per 1M chars), Twilio SIP termination. Re-check these before committing to a budget; the ranking of the profiles does not depend on any single one of them.

Formulas used by the estimator (`voice_agent/estimate.py`):

- telephony = call seconds × (inbound rate + media-streaming rate), rounded up to the increment
- STT = caller-speech seconds × 1.12 (pre/post-roll) × rate when gated, else call seconds × rate; session-billed providers always use call seconds
- TTS = agent-speech minutes × 850 chars × (1 − cache hit rate) × rate
- LLM = Σ over LLM turns of [uncached input × input rate + cache writes × 1.25 × input rate + cache reads × read rate + 60 output tokens × output rate], with the turn's prefix = system prompt + history and caching applied only once the prefix ≥ the model's minimum
- compute = call seconds / (calls per instance × utilisation) × hourly rate
