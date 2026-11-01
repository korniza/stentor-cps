# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Bridge smoke test from inside the container: open the ws, confirm a pty appears and radio_detect routes to it,
echo a few bytes through the pty, then close and confirm cleanup."""
import asyncio, json, os, httpx, websockets

H = {"X-Shack": "1"}
BASE = "http://127.0.0.1:8000/ui/api/"

async def main():
    print("bridge before:", httpx.get(BASE + "bridge", headers=H).json())
    async with websockets.connect("ws://127.0.0.1:8000/ui/ws/serial?shack=1", max_size=None) as ws:
        ready = json.loads(await ws.recv()); print("ready:", ready)
        st = httpx.get(BASE + "bridge", headers=H).json(); print("bridge during:", st)
        assert st["active"] and os.path.exists(st["pty"])
        # bytes the "radio" (browser) sends must show up on the pty slave; bytes written to the slave must reach the ws
        fd = os.open(st["pty"], os.O_RDWR | os.O_NOCTTY)
        await ws.send(b"\x55\xaa radio->host")
        await asyncio.sleep(0.2)
        got = os.read(fd, 100); print("pty got:", got)
        os.write(fd, b"host->radio PROGRAM")
        data = await asyncio.wait_for(ws.recv(), 3); print("ws got:", data)
        os.close(fd)
        # a second client must be refused while the first is active
        try:
            async with websockets.connect("ws://127.0.0.1:8000/ui/ws/serial?shack=1") as ws2:
                await ws2.recv()
            print("second client: NOT refused (bad)")
        except Exception as e:
            print("second client refused:", type(e).__name__, getattr(e, "rcvd", None) or getattr(e, "code", ""))
        # radio_detect must now target the pty (nothing answers, so ok=False, but the routing is what we check)
        r = httpx.post(BASE + "tool/radio_detect", headers=H, json={}, timeout=60).json()
        print("detect via bridge:", r["via_browser_bridge"], r["configured_device"], "|", r["output"][:120].replace("\n", " "))
        await ws.send("ping"); print("pong:", await ws.recv())
    await asyncio.sleep(0.3)
    print("bridge after:", httpx.get(BASE + "bridge", headers=H).json())

asyncio.run(main())
