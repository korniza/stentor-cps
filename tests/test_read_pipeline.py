# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""0.4.64: pipelined reads in the radio relay (bridge.READ_PIPELINE). A fake AnyTone answers R frames after a
simulated round-trip delay; dmrconf is played by feeding commands to the relay and collecting what it returns.
Checks: identical answers, far fewer round trips, fallback to one-at-a-time on a corrupt answer, read-ahead dropped
on a write. Run: python tests/test_read_pipeline.py"""
import asyncio
import os
import sys
import time

from stentor import bridge

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{str(extra)[:300]}]" if extra and not cond else ""))


def mem(addr):                       # deterministic radio memory
    return bytes(((addr >> 4) + i) & 0xFF for i in range(16))


def answer(addr, data=None):
    body = b"W" + addr.to_bytes(4, "big") + b"\x10" + (data if data is not None else mem(addr))
    return body + bytes([sum(body[1:]) & 0xFF, 0x06])


class FakeRadio:
    def __init__(self, relay, rtt, corrupt_at=None, one_at_a_time=False):
        self.relay, self.rtt, self.corrupt_at, self.one = relay, rtt, corrupt_at, one_at_a_time
        self.trips = 0
        self.writes = {}

    async def send(self, data: bytes):
        self.trips += 1
        out = b""
        while data:
            c = data[0]
            if c == ord("R"):
                addr = int.from_bytes(data[1:5], "big")
                a = answer(addr)
                if addr == self.corrupt_at:
                    a = a[:10] + b"\x00" + a[11:]
                    self.corrupt_at = None
                out += a
                data = b"" if self.one else data[6:]      # a radio that drops queued commands
            elif c == ord("W"):
                a = int.from_bytes(data[1:5], "big")
                if a == getattr(self, "wfail_at", None):
                    out += b"\x15"
                else:
                    self.writes[a] = data[6:22]
                    out += b"\x06"
                data = data[24:]
            elif data[:3] == b"END":
                out += b"\x06"
                data = data[3:]
            else:
                data = b""
        async def later():
            await asyncio.sleep(self.rtt)
            self.relay.radio_bytes(out)
        asyncio.get_running_loop().create_task(later())


async def session(pipe, rtt, addrs, corrupt_at=None, extra=None, one=False, browser=False, wfail_at=None):
    bridge.READ_PIPELINE = pipe
    p = bridge._Port(f"t{pipe}{corrupt_at}")
    r, w = os.pipe()
    relay = bridge._Relay(p, None, w)
    radio = FakeRadio(relay, rtt, corrupt_at, one)
    relay._send = radio.send
    if browser:                       # the page does the reads locally and answers in one message
        async def ctl(msg):
            radio.trips += 1
            if msg["type"] == "wb":
                raw, out = bytes.fromhex(msg["hex"]), b""
                for i in range(msg["n"]):
                    f = raw[24 * i: 24 * i + 24]
                    a = int.from_bytes(f[1:5], "big")
                    if a == wfail_at:
                        out += b"\x00" * (msg["n"] - i)
                        break
                    radio.writes[a] = f[6:22]
                    out += b"\x06"

                async def later_w():
                    await asyncio.sleep(rtt)
                    relay.radio_bytes(out)
                asyncio.get_running_loop().create_task(later_w())
                return
            a0, n = msg["addr"], msg["n"]
            out = b""
            for i in range(n):
                a = answer(a0 + 16 * i)
                if a0 + 16 * i == radio.corrupt_at:
                    a = a[:10] + b"\x00" + a[11:]
                    radio.corrupt_at = None
                out += a

            async def later():
                await asyncio.sleep(rtt)
                relay.radio_bytes(out)
            asyncio.get_running_loop().create_task(later())
        relay.send_ctl = ctl
        relay.browser_batch = True
        relay.write_batch = True
        radio.wfail_at = wfail_at
    got = []

    async def to_host(data):
        got.append(bytes(data))
    relay._to_host = to_host
    task = asyncio.get_running_loop().create_task(relay.run())
    t0 = time.monotonic()
    for a in addrs:
        n = len(got)
        relay.cmd_q.put_nowait(b"R" + a.to_bytes(4, "big") + b"\x10")
        while len(got) == n:
            await asyncio.sleep(0)
            await asyncio.sleep(0.0005)
    if extra:
        await extra(relay, got)
    dt = time.monotonic() - t0
    task.cancel()
    os.close(r); os.close(w)
    return got, radio, dt, relay, p


async def main():
    addrs = [0x02000000 + 16 * i for i in range(64)] + [0x02FA0000 + 16 * i for i in range(20)]
    want = [answer(a) for a in addrs]
    g1, r1, t1, _, _ = await session(1, 0.004, addrs)
    g8, r8, t8, rel8, p8 = await session(8, 0.004, addrs)
    check("one at a time: correct", g1 == want)
    check("pipelined: identical answers", g8 == want, (len(g8), len(want)))
    check("pipelined: ~8x fewer round trips", r8.trips <= len(addrs) // 8 + 3 and r1.trips == len(addrs), (r1.trips, r8.trips))
    check("pipelined: faster", t8 < t1 / 3, (round(t1, 3), round(t8, 3)))
    check("run stats count every read", p8.state["run"]["reads"] == len(addrs) and p8.state["run"]["pipelined"] > 0, p8.state["run"])
    gc, rc, _, relc, pc = await session(8, 0.004, addrs, corrupt_at=0x02000000 + 16 * 3)
    check("corrupt answer mid-batch: still identical answers", gc == want)
    check("corrupt answer: pipelining switched off for the session", relc.pipe == 1
          and any(e["kind"] == "pipeline" for e in pc.events))
    gf, rf, _, relf, _ = await session(8, 0.004, addrs, corrupt_at=0x02000000)
    check("corrupt first answer: falls back and recovers", gf == want and relf.pipe == 1)

    go, ro, to, relo, _ = await session(8, 0.004, addrs, one=True)
    check("a radio that ignores queued reads: correct, pipelining off after one batch", go == want and relo.pipe == 1
          and to < 2.0, round(to, 2))

    gb, rb, tb, relb, pb = await session(1, 0.02, addrs, browser=True, one=True)
    check("browser batch: identical answers even when the radio ignores queued commands", gb == want)
    check("browser batch: one WebSocket trip per 64 blocks", rb.trips <= len(addrs) // 64 + 3, rb.trips)
    gbc, rbc, _, relbc, _ = await session(1, 0.002, addrs, browser=True, corrupt_at=0x02000000 + 16 * 5)
    check("browser batch: a corrupt answer is re-read and batching stays on", gbc == want and relbc.browser_batch, relbc.batch_fail)
    # 0.4.75: adaptive batch size
    seq = [0x02000000 + 16 * i for i in range(2000)]
    gs, rs, _, rels, ps = await session(1, 0.002, seq, browser=True)
    check("adaptive: long sequential read grows the batch to the maximum", gs == [answer(a) for a in seq]
          and rels.rb_n == bridge.BROWSER_BATCH_MAX, rels.rb_n)
    check("adaptive: far fewer WebSocket trips than fixed 64", rs.trips <= 2000 // 64 - 10, rs.trips)
    # the D578 pattern: 4 blocks in region A, 4 in region B, the next 4 in A, ... — the read-ahead from A must
    # still answer when dmrconf comes back to it (0.4.75 dropped it on every miss: a D578 read took > 4 minutes)
    inter = []
    for k in range(40):
        inter += [0x009C1E80 + 0x40 * k + 16 * i for i in range(4)] + [0x009C3E80 + 0x40 * k + 16 * i for i in range(4)]
    gi, ri, _, reli, pi = await session(1, 0.002, inter, browser=True)
    check("interleaved regions: identical answers", gi == [answer(a) for a in inter])
    check("interleaved regions: read-ahead kept, few trips", ri.trips <= len(inter) // 16, (ri.trips, len(inter)))
    check("interleaved regions: batch stays within limits", bridge.BROWSER_BATCH <= reli.rb_n <= bridge.BROWSER_BATCH_MAX, reli.rb_n)
    jump = [0x02000000 + 0x10000 * k + 16 * i for k in range(10) for i in range(70)]
    gj, rj, _, relj, _ = await session(1, 0.002, jump, browser=True)
    check("a jump after a fully used batch: back to the base size", gj == [answer(a) for a in jump] and relj.rb_n <= 2 * bridge.BROWSER_BATCH, relj.rb_n)

    async def write_then_read(relay, got):
        n = len(got)
        a = 0x02000010
        frame = b"W" + a.to_bytes(4, "big") + b"\x10" + b"\xAA" * 16
        relay.cmd_q.put_nowait(frame + bytes([sum(frame[1:]) & 0xFF, 0x06]))
        while len(got) == n:
            await asyncio.sleep(0.001)
        check("read-ahead dropped on any other command", not relay.prefetch)
    await session(8, 0.002, addrs[:3], extra=write_then_read)

def wframe(a, data):
    body = b"W" + a.to_bytes(4, "big") + b"\x10" + data
    return body + bytes([sum(body[1:]) & 0xFF, 0x06])


async def writes():
    frames = [wframe(0x02000000 + 16 * i, bytes([i & 0xFF]) * 16) for i in range(150)]

    async def do(relay, got):
        for f in frames:
            n = len(got)
            relay.cmd_q.put_nowait(f)
            while len(got) == n:
                await asyncio.sleep(0.0005)
        n = len(got)
        relay.cmd_q.put_nowait(b"END")
        while len(got) == n:
            await asyncio.sleep(0.001)
    got, radio, dt, relay, p = await session(1, 0.02, [], extra=do, browser=True)
    acks = got[-151:]
    check("batched writes: every write ACKed, END answered", len(acks) == 151 and all(a == b"\x06" for a in acks), acks[-3:])
    check("batched writes: all blocks reached the radio", len(radio.writes) == 150 and radio.writes[0x02000000 + 16 * 149] == bytes([149]) * 16)
    check("batched writes: one WebSocket trip per batch (+END)", radio.trips <= -(-150 // bridge.WRITE_BATCH) + 1, radio.trips)
    check("batched writes: ~1 s instead of 150 round trips", dt < 1.5, round(dt, 2))

    got2, radio2, _, relay2, _ = await session(1, 0.005, [], extra=do, browser=True, wfail_at=0x02000000 + 16 * 70)
    check("a block the radio refuses (also retried alone): the write stops", relay2.wfail is not None, relay2.wfail)
    check("…and END is NAKed, never sent to the radio", got2[-1] == b"\x15", got2[-3:])
    check("…blocks before the failure were written", 0x02000000 + 16 * 69 in radio2.writes)



async def guard():
    """dmrconf waited longer than its limit after writes began -> END is refused (radio keeps its old content)."""
    bridge.HOST_WAIT_GUARD = 0.05
    frames = [wframe(0x02000000 + 16 * i, b"\x01" * 16) for i in range(3)]

    async def do(relay, got):
        relay.write_batch = False                     # plain path
        relay.browser_batch = False
        slow = relay._send

        async def send_slow(data):
            await asyncio.sleep(0.12 if data[:1] == b"W" and data[1:5] == frames[1][1:5] else 0)
            await slow(data)
        relay._send = send_slow
        for f in frames + [b"END"]:
            n = len(got)
            relay.cmd_q.put_nowait(f)
            while len(got) == n:
                await asyncio.sleep(0.001)
    got, radio, _, relay, _ = await session(1, 0.001, [], extra=do)
    check("guard: a too-slow write answer stops the session", relay.wfail and "waited" in relay.wfail, relay.wfail)
    check("guard: END refused, never sent", got[-1] == b"\x15", got[-2:])
    bridge.HOST_WAIT_GUARD = 3.0



def progress_bar():
    import tempfile, time as _t
    from pathlib import Path
    from stentor import config
    config.DATA_DIR = Path(tempfile.mkdtemp())
    bridge._sizes = None
    p = bridge.port("pbar")
    p.state["run"] = {"started": _t.time() - 10, "reads": 5000, "writes": 0, "radio": "D578UV"}
    pr = bridge.progress("pbar")
    check("progress: % from the D578 default size", pr.get("pct") == int(100 * 5000 / 15053) and pr.get("eta_s", 0) > 0, pr)
    bridge._remember_size({"radio": "D578UV", "reads": 10000, "writes": 0})
    check("progress: a completed read becomes the new 100 %", bridge.progress("pbar")["pct"] == 50)
    bridge._remember_size({"radio": "D578UV", "reads": 12, "writes": 0})
    check("progress: a detect (tiny session) is not remembered", bridge.expected_blocks("D578UV", "read") == 10000)
    p.state["run"] = {"started": _t.time() - 10, "reads": 859, "writes": 15455 // 2, "radio": "D578UV"}
    check("progress: writes use the write size", bridge.progress("pbar")["pct"] == 49, bridge.progress("pbar"))
    p.state["run"] = {"started": _t.time(), "reads": 10, "radio": "XYZ"}
    check("progress: unknown radio -> no %", "pct" not in bridge.progress("pbar"))

asyncio.run(main())
asyncio.run(writes())
progress_bar()
asyncio.run(guard())
print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
