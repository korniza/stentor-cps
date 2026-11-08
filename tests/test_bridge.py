# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Browser-bridge smoke test against a live single-operator server (run inside the container, e.g. via
tools/live-tests.sh). Rewritten for the relay (0.4.x): bytes are no longer piped raw — the host's commands go to the
"radio" (the WebSocket) and the radio's answers come back on the pty. Checks: ready + pty, host → radio, radio → host,
ping counters, same-user takeover (old page closed with 4410), radio_detect routed through the bridge, cleanup.
The full read/write protocol with corrupted frames is covered by tests/test_relay.py."""
import asyncio
import json
import os
import select

import httpx
import websockets

H = {"X-Shack": "1"}
BASE = "http://127.0.0.1:8000/ui/api/"
WS = "ws://127.0.0.1:8000/ui/ws/serial?shack=1"
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print("PASS" if cond else "FAIL", name, "" if cond else extra)


def pty_read(fd, timeout=3.0) -> bytes:
    r, _, _ = select.select([fd], [], [], timeout)
    return os.read(fd, 4096) if r else b""


async def main():
    check("no bridge before", not httpx.get(BASE + "bridge", headers=H).json().get("active"))
    async with websockets.connect(WS, max_size=None) as ws:
        ready = json.loads(await ws.recv())
        check("ready with a pty", ready.get("type") == "ready" and ready.get("pty"), ready)
        st = httpx.get(BASE + "bridge", headers=H).json()
        check("status: active browser bridge", st.get("active") and st.get("transport") == "browser" and os.path.exists(st.get("pty", "")), st)

        fd = os.open(st["pty"], os.O_RDWR | os.O_NOCTTY)
        os.write(fd, b"PROGRAM")                                   # AnyTone: enter programming mode
        got = await asyncio.wait_for(ws.recv(), 5)
        check("host → radio: PROGRAM reaches the WebSocket", isinstance(got, bytes) and got.startswith(b"PROGRAM"), got)
        await ws.send(b"QX\x06")                                   # the radio's answer
        back = await asyncio.get_running_loop().run_in_executor(None, pty_read, fd)
        check("radio → host: the answer reaches the pty", back.startswith(b"QX\x06"), back)
        os.close(fd)

        await ws.send("ping")
        pong = json.loads(await asyncio.wait_for(ws.recv(), 3))
        check("ping → pong with counters", pong.get("type") == "pong" and pong.get("to_radio", 0) >= 7, pong)

        # the same user opening the radio in a second page takes it over; the first page is told with 4410
        async with websockets.connect(WS, max_size=None) as ws2:
            r2 = json.loads(await ws2.recv())
            check("second page takes over", r2.get("type") == "ready", r2)
            try:
                await asyncio.wait_for(ws.recv(), 3)
                check("first page closed with 4410", False, "still open")
            except websockets.ConnectionClosed as e:
                check("first page closed with 4410", e.rcvd and e.rcvd.code == 4410, e)

            # radio_detect must target the bridge's pty (nothing answers like a real radio — routing is what matters)
            r = httpx.post(BASE + "tool/radio_detect", headers=H, json={}, timeout=90).json()
            check("radio_detect routed through the bridge", r.get("via_browser_bridge") is True, {k: r.get(k) for k in ("via_browser_bridge", "configured_device")})
    await asyncio.sleep(0.5)
    check("bridge gone after the page closes", not httpx.get(BASE + "bridge", headers=H).json().get("active"))


asyncio.run(main())
print(f"{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
