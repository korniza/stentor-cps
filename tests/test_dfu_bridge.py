# SPDX-License-Identifier: AGPL-3.0-or-later
"""DFU bridge (TyT family through WebUSB): dmrconf's control transfers go over a unix socket to dfubridge.DfuSession,
which sends them to the page; here a fake page runs them on tests/fake_tyt.FakeTyT.

  Part A (always): the socket protocol, GETSTATUS folded into UPLOAD/DNLOAD, read-ahead, counters, identify, errors,
                   page gone, session cleanup.
  Part B (when the dmrconf on PATH has the dfu-socket patch, e.g. in the stentor image ≥ 0.4.84):
                   real dmrconf: detect, write --init-codeplug, read --yaml (same channels back), update-mode write,
                   no write to un-erased flash.
  Part C (always): the real WebSocket route /ui/ws/serial?dfu=1 (link, takeover).
Run:  PYTHONPATH=. DATA_DIR=/tmp/t python tests/test_dfu_bridge.py
"""
import asyncio
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fake_tyt import FakeTyT  # noqa: E402

from stentor import bridge, dfubridge  # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{str(extra)[:400]}]" if extra and not cond else ""))


class FakePage:
    """Stands in for the browser: runs each op on the fake radio, answers {"type":"dfur"} like shack.html."""

    def __init__(self, radio: FakeTyT, delay: float = 0.002):
        self.radio, self.delay, self.session, self.msgs, self.gone = radio, delay, None, [], False

    async def send_json(self, msg):
        if self.gone:
            raise RuntimeError("websocket closed")
        self.msgs.append(msg)
        if msg.get("type") != "dfu":
            return

        async def answer():
            await asyncio.sleep(self.delay)
            res = []
            for o in msg["ops"]:
                st, data = self.radio.ctrl(o["rt"], o["rq"], o["v"], o["i"], o["n"], bytes.fromhex(o.get("hex", "")))
                res.append({"st": st, "hex": data.hex()})
                if st < 0:
                    break
            self.session.on_result({"type": "dfur", "id": msg["id"], "res": res})
        asyncio.get_running_loop().create_task(answer())


async def xfer(reader, writer, rt, rq, v, ln, data=b""):
    writer.write(b"C" + bytes([rt, rq]) + struct.pack("<HHH", v, 0, ln) + data)
    await writer.drain()
    st = struct.unpack("<i", await reader.readexactly(4))[0]
    body = await reader.readexactly(st) if (rt & 0x80) and st > 0 else b""
    return st, body


async def part_a():
    radio = FakeTyT("DM-1701")
    radio.mem[0x2000:0x2000 + 4096] = bytes(range(256)) * 16
    p = bridge._Port("dfu-a")
    page = FakePage(radio)
    s = dfubridge.DfuSession(p, page, "test")
    page.session = s
    await s.start()
    check("link is dfu:<socket>", s.link.startswith("dfu:") and os.path.exists(s.path), s.link)
    check("socket is private (0600)", (os.stat(s.path).st_mode & 0o777) == 0o600, oct(os.stat(s.path).st_mode))
    r, w = await asyncio.open_unix_connection(s.path)

    st, body = await xfer(r, w, 0xA1, 5, 0, 1)                    # GETSTATE
    check("GETSTATE through the page", st == 1 and body == bytes([2]), (st, body))
    st, _ = await xfer(r, w, 0x21, 1, 0, 2, b"\xa2\x01")            # identify mode
    check("DNLOAD command accepted", st == 2, st)
    n_msgs = len(page.msgs)
    st, body = await xfer(r, w, 0xA1, 3, 0, 6)                    # GETSTATUS — must come from the cache
    check("GETSTATUS after DNLOAD served without a round trip", st == 6 and len(page.msgs) == n_msgs, (st, len(page.msgs), n_msgs))
    await xfer(r, w, 0xA1, 5, 0, 1)
    await xfer(r, w, 0x21, 6, 0, 0)                               # ABORT
    st, body = await xfer(r, w, 0xA1, 2, 0, 64)                   # identify
    await xfer(r, w, 0xA1, 3, 0, 6)
    check("identify string", st == 64 and body.startswith(b"DM-1701\x00"), body[:16])
    check("run records the radio", (p.state.get("run") or {}).get("radio") == "DM-1701", p.state.get("run"))

    # sequential block reads: 0x2000 = block 8 (+2) with 1 KB blocks
    before = len(page.msgs)
    got = b""
    for k in range(40):
        st, body = await xfer(r, w, 0xA1, 2, 10 + k, 1024)
        st2, _ = await xfer(r, w, 0xA1, 3, 0, 6)
        if st != 1024 or st2 != 6:
            break
        got += body
    trips = len(page.msgs) - before
    check("40 sequential 1 KB reads", len(got) == 40 * 1024, len(got))
    check("block data correct", got[:4096] == bytes(range(256)) * 16 and got[4096:8192] == b"\xff" * 4096)
    check(f"read-ahead: {trips} page round trips for 40 blocks", 2 <= trips <= 4, trips)
    check("run counts reads", (p.state.get("run") or {}).get("reads") == 40, p.state.get("run"))

    # a DNLOAD in between drops the read-ahead; data write lands in flash
    await xfer(r, w, 0x21, 1, 0, 5, b"\x41" + (0x10000).to_bytes(4, "little"))    # erase page 0x10000
    await xfer(r, w, 0xA1, 3, 0, 6)
    await xfer(r, w, 0xA1, 5, 0, 1)
    await xfer(r, w, 0x21, 6, 0, 0)
    blk = bytes([0x5A]) * 1024
    st, _ = await xfer(r, w, 0x21, 1, 0x10000 // 1024 + 2, 1024, blk)
    await xfer(r, w, 0xA1, 3, 0, 6)
    check("block write reached the radio", st == 1024 and radio.mem[0x10000:0x10400] == blk, st)
    check("run counts writes", (p.state.get("run") or {}).get("writes") == 1, p.state.get("run"))
    check("write went to erased flash", radio.unerased_writes == 0, radio.unerased_writes)
    st, body = await xfer(r, w, 0xA1, 2, 0x10000 // 1024 + 2, 1024)
    await xfer(r, w, 0xA1, 3, 0, 6)
    check("read after write sees the new data (no stale read-ahead)", body == blk)

    # an error from the radio is passed on as a libusb error
    st, _ = await xfer(r, w, 0xA1, 2, 1, 16)                      # block 1 is invalid on the fake
    check("radio error -> negative status", st == dfubridge.ERR_PIPE, st)
    w.close()
    for _ in range(50):
        if p.state.get("last_run"):
            break
        await asyncio.sleep(0.02)
    lr = p.state.get("last_run") or {}
    check("dmrconf disconnect ends the run (last_run)", lr.get("ended") and lr.get("reads") == 41 and lr.get("dfu"), lr)

    # page gone: dmrconf gets ERR_NO_DEVICE instead of hanging
    page.gone = True
    r, w = await asyncio.open_unix_connection(s.path)
    t0 = time.monotonic()
    st, _ = await xfer(r, w, 0xA1, 5, 0, 1)
    check("page gone -> error at once", st == dfubridge.ERR_NO_DEVICE and time.monotonic() - t0 < 2, st)
    w.close()

    # timeout: the page never answers
    page.gone = False
    old = dfubridge.OP_TIMEOUT
    dfubridge.OP_TIMEOUT = 0.3

    async def silent(msg):
        page.msgs.append(msg)
    page.send_json, keep = silent, page.send_json
    r, w = await asyncio.open_unix_connection(s.path)
    st, _ = await xfer(r, w, 0xA1, 5, 0, 1)
    check("no answer -> timeout status", st == dfubridge.ERR_TIMEOUT, st)
    w.close()
    dfubridge.OP_TIMEOUT = old
    page.send_json = keep

    s.close()
    check("close removes the socket dir", not os.path.exists(s.dir))
    check("close frees the port", p.session is None and not p.state.get("active"))


CODEPLUG = """---
version: 0.15.1
settings: {defaultID: id1, introLine1: STENTOR, introLine2: DFU TEST}
radioIDs:
  - dmr: {id: id1, name: N0CALL, number: 1234567}
contacts:
  - dmr: {id: cont1, name: Greece, type: GroupCall, number: 202}
  - dmr: {id: cont2, name: Local, type: GroupCall, number: 9}
groupLists:
  - {id: grp1, name: BM Greece, contacts: [cont1, cont2]}
channels:
  - dmr: {id: ch1, name: HS 202 TS2, rxFrequency: 438.4 MHz, txFrequency: 438.4 MHz, colorCode: 1, timeSlot: TS2, contact: cont1, groupList: grp1}
  - dmr: {id: ch2, name: HS 9 TS2, rxFrequency: 438.4 MHz, txFrequency: 438.4 MHz, colorCode: 1, timeSlot: TS2, contact: cont2, groupList: grp1}
  - fm: {id: ch3, name: 2m Call, rxFrequency: 145.5 MHz, txFrequency: 145.5 MHz}
zones:
  - {id: zone1, name: Hotspot, A: [ch1, ch2]}
  - {id: zone2, name: Simplex, A: [ch3]}
scanLists: []
"""


def dmrconf_has_dfu() -> bool:
    """The dmrconf on PATH carries the dfu-socket patch (its library contains the bridge's log text)."""
    if os.environ.get("DFU_DMRCONF") == "1":
        return True
    exe = shutil.which("dmrconf")
    if not exe:
        return False
    try:
        libs = subprocess.run(["ldd", exe], capture_output=True, text=True, timeout=20).stdout
        paths = [exe] + [ln.split("=>")[1].split()[0] for ln in libs.splitlines() if "libdmrconf" in ln and "=>" in ln]
        return any(b"DFU bridge" in Path(x).read_bytes() for x in paths if Path(x).exists())
    except Exception:  # noqa: BLE001
        return False


async def dmrconf(*args, timeout=600):
    env = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8", QT_QPA_PLATFORM="offscreen")
    proc = await asyncio.create_subprocess_exec("dmrconf", *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode(errors="replace")


async def part_b():
    if not dmrconf_has_dfu():
        print("SKIP part B: dmrconf without the dfu-socket patch")
        return
    tmp = Path(tempfile.mkdtemp(prefix="dfu-b-"))
    radio = FakeTyT("DM-1701")
    p = bridge._Port("dfu-b")
    page = FakePage(radio, delay=0.001)
    s = dfubridge.DfuSession(p, page, "test")
    page.session = s
    await s.start()
    dev = ["--radio", "dm1701", "--device", s.link]

    code, out = await dmrconf("detect", *dev, timeout=60)
    check("dmrconf detect over the DFU bridge", code == 0 and "1701" in out, out[-500:])
    check("detect: radio entered programming mode", radio.programming)

    src = tmp / "in.yaml"
    src.write_text(CODEPLUG)
    t0 = time.monotonic()
    code, out = await dmrconf("write", "--yaml", "--init-codeplug", *dev, str(src))
    check("dmrconf write --init-codeplug", code == 0, out[-800:])
    print(f"     write took {time.monotonic() - t0:.1f}s, pages erased {len(radio.erased_pages)}, last_run {p.state.get('last_run')}")
    check("no write to un-erased flash", radio.unerased_writes == 0, radio.unerased_writes)

    dst = tmp / "out.yaml"
    t0 = time.monotonic()
    code, out = await dmrconf("read", "--yaml", *dev, str(dst))
    check("dmrconf read --yaml", code == 0 and dst.exists(), out[-800:])
    text = dst.read_text() if dst.exists() else ""
    print(f"     read took {time.monotonic() - t0:.1f}s, last_run {p.state.get('last_run')}")
    check("channels came back", all(n in text for n in ("HS 202 TS2", "HS 9 TS2", "2m Call")), text[:600])
    check("contact 202 came back", "Greece" in text and "202" in text)
    lr = p.state.get("last_run") or {}
    check("read counted 824 blocks", lr.get("reads") == 824, lr)

    # update mode: qdmr reads first, then erases and writes
    upd = tmp / "upd.yaml"
    upd.write_text(text.replace("2m Call", "2m Calling"))
    code, out = await dmrconf("write", "--yaml", *dev, str(upd))
    check("dmrconf write (update mode)", code == 0, out[-800:])
    lr = p.state.get("last_run") or {}
    check("update write: reads + writes counted", lr.get("reads") == 824 and lr.get("writes") == 824, lr)
    code, out = await dmrconf("read", "--yaml", *dev, str(tmp / "back.yaml"))
    back = (tmp / "back.yaml").read_text() if (tmp / "back.yaml").exists() else ""
    check("edited name read back", code == 0 and "2m Calling" in back, out[-400:])
    check("still no write to un-erased flash", radio.unerased_writes == 0, radio.unerased_writes)

    # wrong radio on the cable: an MD-UV390 while dm1701 is selected -> qdmr still talks, Stentor flags it
    check("ident match helper", dfubridge.ident_matches("dm1701", "DM-1701") is True
          and dfubridge.ident_matches("dm1701", "MD-UV390") is False and dfubridge.ident_matches("dm1701", "???") is None)
    s.close()
    shutil.rmtree(tmp, ignore_errors=True)


def part_c():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from stentor import ui
    ck = {}
    radio = FakeTyT("DM-1701")
    stop = threading.Event()

    def page(ws):
        while not stop.is_set():
            try:
                m = ws.receive_json()
            except Exception:  # noqa: BLE001
                return
            if m.get("type") != "dfu":
                continue
            res = []
            for o in m["ops"]:
                st, data = radio.ctrl(o["rt"], o["rq"], o["v"], o["i"], o["n"], bytes.fromhex(o.get("hex", "")))
                res.append({"st": st, "hex": data.hex()})
                if st < 0:
                    break
            ws.send_json({"type": "dfur", "id": m["id"], "res": res})

    async def ask(path):
        r, w = await asyncio.open_unix_connection(path)
        st, body = await xfer(r, w, 0xA1, 5, 0, 1)
        w.close()
        return st, body

    with TestClient(ui.app) as cl:
        port = bridge.port()
        with cl.websocket_connect("/ui/ws/serial?shack=1&dfu=1", headers=ck) as ws:
            ready = ws.receive_json()
            check("DFU bridge ready", ready.get("type") == "ready" and ready.get("dfu"), ready)
            ws.send_json({"type": "hello", "dfu": 1, "vid": 0x0483, "pid": 0xDF11, "ua": "test", "product": "Digital Radio in USB mode"})
            link = port.bridge_link or ""
            check("bridge link is a dfu socket", link.startswith("dfu:") and os.path.exists(link[4:]), link)
            th = threading.Thread(target=page, args=(ws,), daemon=True)
            th.start()
            st, body = asyncio.run(ask(link[4:]))
            check("control transfer through the real WebSocket", st == 1 and body == bytes([2]), (st, body))
            check("device recorded from hello", (port.device or {}).get("usb") == "0483:DF11" and (port.device or {}).get("dfu"), port.device)
            stj = cl.get("/ui/api/bridge", headers={"X-Shack": "1", **ck}).json()
            check("bridge status: active + dfu", stj.get("active") and stj.get("bridge") and stj.get("dfu"), stj)

            # second tab: takes over (old page closed with 4410)
            with cl.websocket_connect("/ui/ws/serial?shack=1&dfu=1", headers=ck) as ws2:
                ready2 = ws2.receive_json()
                link2 = port.bridge_link or ""
                check("second tab takes over", ready2.get("type") == "ready" and link2.startswith("dfu:") and link2 != link, (link, link2))
                check("old socket removed on takeover", not os.path.exists(link[4:]))
            stop.set()
        for _ in range(50):
            if not port.bridge_link:
                break
            time.sleep(0.05)
        check("page closed -> link gone", not port.bridge_link)


async def main():
    await part_a()
    await part_b()


asyncio.run(main())
part_c()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
