"""A fake caller for the generic WebSocket transport (and a crude load generator).

Streams bursts of 'speech' (a tone) separated by silence, in real time, and counts
the audio the agent sends back. Use against ``voice-agent serve --mock``.

    python examples/ws_client.py ws://localhost:8765/ --turns 4
    python examples/ws_client.py ws://localhost:8765/ --turns 3 --concurrency 20   # 20 simultaneous callers
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import websockets

sys.path.insert(0, ".")
from voice_agent.audio import PCM16_16K, frame_bytes, silence, tone  # noqa: E402


async def caller(url: str, turns: int, idx: int) -> None:
    fmt = PCM16_16K
    fb = frame_bytes(fmt)
    received = 0
    clears = 0
    t0 = time.monotonic()
    async with websockets.connect(url) as ws:

        async def reader() -> None:
            nonlocal received, clears
            async for msg in ws:
                if isinstance(msg, bytes):
                    received += len(msg)
                else:
                    ctl = json.loads(msg)
                    if ctl.get("type") == "clear":
                        clears += 1
                    elif ctl.get("type") == "hangup":
                        return

        rt = asyncio.create_task(reader())

        async def stream(audio: bytes) -> None:
            for i in range(0, len(audio) - fb + 1, fb):
                await ws.send(audio[i : i + fb])
                await asyncio.sleep(0.02)

        await stream(silence(fmt, 4.5))  # let the greeting play
        for _ in range(turns):
            await stream(tone(fmt, 1.5, freq=180.0, amplitude=0.4))  # "speech"
            await stream(silence(fmt, 6.0))  # wait for the reply
        await ws.send(json.dumps({"type": "hangup"}))
        try:
            await asyncio.wait_for(rt, timeout=5)
        except (TimeoutError, websockets.ConnectionClosed):
            pass
    print(
        f"caller {idx}: {time.monotonic() - t0:.1f}s, agent audio received {fmt.seconds_for(received):.1f}s, clears={clears}"
    )


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("url", nargs="?", default="ws://localhost:8765/")
    p.add_argument("--turns", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=1)
    a = p.parse_args()
    await asyncio.gather(*(caller(a.url, a.turns, i) for i in range(a.concurrency)))


if __name__ == "__main__":
    asyncio.run(main())
