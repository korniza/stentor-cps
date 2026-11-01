# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Differential write + USB-style relay, no server needed: a fake AnyTone answers through the relay's send callback,
a fake dmrconf on the pty does a read session, then a write session where only a few blocks differ. The radio must
receive only the changed blocks, the cache must be dropped on an aborted write, and a foreign radio must reset it."""
import asyncio
import os
import random
import select
import sys
import tty as _tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DIFF_WRITE"] = "1"          # the feature under test; off by default since 0.4.1 (AnyTone sector erase)
from stentor import bridge  # noqa: E402

MEM = bytearray(random.Random(7).randbytes(2048))
RADIO_ID = b"ID878UV2\x03V100\x00\x00\x06"
received_writes: list[int] = []


class FakeRadio:
    def __init__(self, session_ref, radio_id=RADIO_ID):
        self.buf = b""
        self.session_ref = session_ref
        self.radio_id = radio_id

    async def send(self, data: bytes) -> None:
        self.buf += data
        while self.buf:
            c = self.buf[0]
            need = {0x50: 7, 0x02: 1, 0x52: 6, 0x57: 24, 0x45: 3}[c]
            if len(self.buf) < need:
                return
            frame, self.buf = self.buf[:need], self.buf[need:]
            if c == 0x50:
                resp = b"QX\x06"
            elif c == 0x02:
                resp = self.radio_id
            elif c == 0x52:
                a = int.from_bytes(frame[1:5], "big")
                body = b"W" + frame[1:5] + b"\x10" + bytes(MEM[a:a + 16])
                resp = body + bytes([sum(body[1:]) & 0xFF, 0x06])
            elif c == 0x57:
                a = int.from_bytes(frame[1:5], "big")
                received_writes.append(a)
                MEM[a:a + 16] = frame[6:22]
                resp = b"\x06"
            else:
                resp = b"\x06"
            await asyncio.sleep(0)
            self.session_ref[0].radio_bytes(resp)


def host(ptyname: str, blocks: int, writes: dict[int, bytes], abort: bool = False) -> None:
    fd = os.open(ptyname, os.O_RDWR | os.O_NOCTTY)
    _tty.setraw(fd)

    def xfer(cmd, rlen):
        os.write(fd, cmd)
        out = b""
        while len(out) < rlen:
            r, _, _ = select.select([fd], [], [], 4.0)
            if not r:
                raise TimeoutError(f"timeout after {out.hex()}")
            out += os.read(fd, rlen - len(out))
        return out

    assert xfer(b"PROGRAM", 3) == b"QX\x06"
    assert xfer(b"\x02", 16)[0:1] == b"I"
    for i in range(blocks):
        addr = i * 16
        r = xfer(b"R" + addr.to_bytes(4, "big") + b"\x10", 24)
        assert r[6:22] == bytes(MEM[addr:addr + 16])
    for addr, data in writes.items():
        body = b"W" + addr.to_bytes(4, "big") + b"\x10" + data
        assert xfer(body + bytes([sum(body[1:]) & 0xFF, 0x06]), 1) == b"\x06"
    if not abort:
        assert xfer(b"END", 1) == b"\x06"
    os.close(fd)


async def session(fn):
    ref = [None]
    radio = FakeRadio(ref)
    s = bridge._PtySession(radio.send, "test", "t")
    ref[0] = s
    try:
        await asyncio.get_running_loop().run_in_executor(None, fn, s.link)
    finally:
        s.close()
    return bridge.last_run()


async def main():
    # 1. read everything, then write it back unchanged except 3 blocks
    r = await session(lambda p: host(p, 128, {}))
    assert r["reads"] == 128 and r["writes"] == 0, r
    assert len(bridge._cache.blocks) == 128
    image = {i * 16: bytes(MEM[i * 16:i * 16 + 16]) for i in range(128)}
    changed = {16 * 5: bytes(16), 16 * 77: b"\xff" * 16, 16 * 127: bytes(range(16))}
    image.update(changed)
    received_writes.clear()
    r = await session(lambda p: host(p, 0, image))
    assert sorted(received_writes) == sorted(changed), received_writes
    assert r["writes"] == 3 and r["skipped"] == 125, r
    # 2. the cache follows the writes: writing the same image again sends nothing
    received_writes.clear()
    r = await session(lambda p: host(p, 0, image))
    assert received_writes == [] and r["skipped"] == 128, r
    # 3. an aborted write session (no END) drops the cache -> next write goes fully to the radio
    received_writes.clear()
    await session(lambda p: host(p, 0, {16 * 3: b"\x01" * 16}, abort=True))
    assert bridge._cache.blocks == {}, "cache should be dropped after an abort"
    r = await session(lambda p: host(p, 0, image))
    assert r["skipped"] == 0 and r["writes"] == 128, r
    # 4. a different radio identity resets the cache
    await session(lambda p: host(p, 8, {}))
    ref = [None]
    radio = FakeRadio(ref, radio_id=b"ID868UVE\x03V100\x00\x00\x06")
    s = bridge._PtySession(radio.send, "test", "t")
    ref[0] = s
    try:
        await asyncio.get_running_loop().run_in_executor(None, host, s.link, 2, {})
    finally:
        s.close()
    assert len(bridge._cache.blocks) == 2 and bridge._cache.radio_id.startswith(b"ID868"), bridge._cache.radio_id
    # 5. blocks never read are never skipped and never cached
    received_writes.clear()
    r = await session(lambda p: host(p, 0, {0x10000: b"\x55" * 16}))
    assert received_writes == [0x10000] and 0x10000 not in bridge._cache.blocks
    print("diff-write OK:", r)
    # 6. overlay: planned reads after identify, planned writes before END, via direct_session on a "browser" bridge
    from stentor import fmradio as fm
    ref = [None]
    radio = FakeRadio(ref)
    s = bridge._PtySession(radio.send, "browser", "t")
    ref[0] = s
    try:
        MEM.extend(bytes(0x02480230 + 0x40 - len(MEM)))      # grow the fake memory to the WFM area
        want = [{"mhz": 88.6, "name": "a"}, {"mhz": 107.5, "name": "b"}, {"mhz": 95.2, "name": "c"}]
        res = await bridge.direct_session(reads=[fm.WFM_VFO], writes=fm.encode_blocks(want))
        assert res["writes"]["sent"] == 35 and not res["writes"]["failed"], res
        res = await bridge.direct_session(reads=fm.read_addresses())
        got = fm.decode_blocks(res["reads"])
        assert [x["mhz"] for x in got["stations"]] == [88.6, 107.5, 95.2], got
        res = await bridge.direct_session(writes=fm.encode_blocks(want, keep_vfo=False))   # same again: all skipped via cache
        assert res["writes"]["sent"] == 0 and res["writes"]["skipped"] == 34, res
        # keep blocks: the VFO is read in-session and written back byte-identical
        MEM[fm.WFM_VFO:fm.WFM_VFO + 16] = bytes.fromhex("00 00 88 00") + bytes(12)
        bridge._cache.invalidate("test")
        received_writes.clear()
        res = await bridge.direct_session(reads=[fm.WFM_VFO], writes={fm.WFM_VFO: None, fm.WFM_LIST: bytes(16)})
        assert fm.WFM_VFO in received_writes and res["writes"]["sent"] == 2 and not res["writes"]["failed"], res
        assert bytes(MEM[fm.WFM_VFO:fm.WFM_VFO + 4]) == bytes.fromhex("00 00 88 00")
    finally:
        s.close()
    print("overlay OK:", got)


asyncio.run(main())
