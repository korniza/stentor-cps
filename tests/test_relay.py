# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Relay test, run inside the container: a fake AnyTone behind the WebSocket that corrupts every Nth response the way
Windows Web Serial was seen to (drops 2 bytes, leaves stale ones), while a fake dmrconf on the pty side does a
read + write pass and must see only valid frames."""
import asyncio, json, os, random, httpx, websockets

H = {"X-Shack": "1"}; BASE = "http://127.0.0.1:8000/ui/api/"
MEM = bytearray(random.Random(1).randbytes(4096))
CORRUPT_EVERY = 37

def read_resp(addr):
    body = bytes([0x57]) + addr.to_bytes(4, "big") + b"\x10" + bytes(MEM[addr:addr + 16])
    return body + bytes([sum(body[1:]) & 0xFF, 0x06])

async def fake_radio(ws, stop):
    n = 0
    buf = b""
    while not stop.is_set():
        try:
            m = await asyncio.wait_for(ws.recv(), 0.2)
        except asyncio.TimeoutError:
            continue
        if isinstance(m, str):
            continue
        buf += m
        while buf:
            c = buf[0]
            need = {0x50: 7, 0x02: 1, 0x52: 6, 0x57: 24, 0x45: 3}[c]
            if len(buf) < need:
                break
            frame, buf = buf[:need], buf[need:]
            if c == 0x50: resp = b"QX\x06"
            elif c == 0x02: resp = b"ID878UV2\x03V100\x00\x00\x06"
            elif c == 0x52:
                resp = read_resp(int.from_bytes(frame[1:5], "big"))
            elif c == 0x57:
                a = int.from_bytes(frame[1:5], "big")
                if (sum(frame[1:22]) & 0xFF) == frame[22]:
                    MEM[a:a + 16] = frame[6:22]
                resp = b"\x06"
            else: resp = b"\x06"
            n += 1
            if c == 0x52 and n % 101 == 0:
                continue  # radio never answers (lost on the Windows side): relay must time out and retry
            if c == 0x52 and n % CORRUPT_EVERY == 0:
                # mimic the observed mangling: 'W' alone, then bytes 1-2 lost, two stale bytes, rest, split 20+3
                bad = resp[0:1] + resp[3:6] + b"\xe0\x10" + resp[6:21] + resp[21:]
                await ws.send(bad[:1]); await ws.send(bad[1:21]); await ws.send(bad[21:])
            else:
                await ws.send(resp[:1]); await ws.send(resp[1:])

def host_side(ptyname, n_frames):
    """Synchronous fake dmrconf: strict request/response on the pty."""
    import termios, tty as _tty, select
    fd = os.open(ptyname, os.O_RDWR | os.O_NOCTTY)
    _tty.setraw(fd)
    def xfer(cmd, rlen):
        os.write(fd, cmd); out = b""
        while len(out) < rlen:
            r, _, _ = select.select([fd], [], [], 4.0)
            if not r: raise TimeoutError(f"timeout after {out.hex()}")   # 1 s like stock dmrconf; patched build waits 4 s
            out += os.read(fd, rlen - len(out))
        return out
    assert xfer(b"PROGRAM", 3) == b"QX\x06"
    assert xfer(b"\x02", 16)[0:1] == b"I"
    bad = 0
    for i in range(n_frames):
        addr = (i * 16) % 4096
        r = xfer(b"R" + addr.to_bytes(4, "big") + b"\x10", 24)
        if r[1:5] != addr.to_bytes(4, "big") or r[6:22] != bytes(MEM[addr:addr + 16]) or r[23] != 6:
            bad += 1
    data = bytes(range(16))
    for i in range(40):
        addr = i * 16
        body = b"W" + addr.to_bytes(4, "big") + b"\x10" + data
        assert xfer(body + bytes([sum(body[1:]) & 0xFF, 0x06]), 1) == b"\x06"
        assert bytes(MEM[addr:addr + 16]) == data   # the fake radio applies writes immediately (the real one does not)
    assert xfer(b"END", 1) == b"\x06"
    os.close(fd)
    return bad

async def main():
    async with websockets.connect("ws://127.0.0.1:8000/ui/ws/serial?shack=1", max_size=None) as ws:
        ready = json.loads(await ws.recv())
        stop = asyncio.Event()
        radio_task = asyncio.create_task(fake_radio(ws, stop))
        bad = await asyncio.get_running_loop().run_in_executor(None, host_side, ready["pty"], 600)
        stop.set(); await radio_task
        st = httpx.get(BASE + "bridge", headers=H).json()
        print("bad frames seen by host:", bad, "| retries:", st["retries"], "verify_failures:", st["verify_failures"], "transparent:", st["transparent"])
        assert bad == 0 and st["retries"] >= 600 // CORRUPT_EVERY - 1
    print("relay OK")

asyncio.run(main())
