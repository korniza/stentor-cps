# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""DFU bridge: TyT-family radios (TYT MD-UV380/390, MD-390, MD-2017, Retevis RT3S, Baofeng DM-1701 …) through the
user's browser.

EXPERIMENTAL: so far tested only against a simulator (tests/fake_tyt.py) and the patched dmrconf, not yet with a
real TyT-family radio. Read first and keep that backup before trying a Write; reports welcome.

These radios are not serial ports: they are programmed with USB DFU control transfers (VID:PID 0483:DF11). The page
opens the radio with WebUSB; Stentor's dmrconf build (patch qdmr-0.15.1-dfu-socket.patch) is started with
``--device dfu:<unix socket>`` and sends every control transfer over that socket; this module forwards it to the page
over the user's bridge WebSocket and returns the answer:

    dmrconf --socket--> DfuSession --WebSocket {"type":"dfu","id","ops":[…]}--> page (WebUSB) --> radio
            <-------- status+data  <-------- {"type":"dfur","id","res":[{st,hex}]} <------

Socket frames (little endian):  request  'C' bmRequestType bRequest wValue(2) wIndex(2) wLength(2) [data out]
                                answer   status(int32: bytes done, or a negative libusb error) [data in]

Speed: qdmr follows every UPLOAD/DNLOAD with GETSTATUS, so the page runs the pair in one round trip and the
GETSTATUS answer is kept for dmrconf's next request (the radio sees exactly the same sequence). Sequential block
reads are read ahead in batches (UPLOAD with an explicit block number is a plain flash read on these radios — no
cursor, no side effect); any other request drops the read-ahead."""
import asyncio
import os
import shutil
import struct
import tempfile
import time

# USB DFU class requests (DFU 1.1)
DNLOAD, UPLOAD, GETSTATUS, GETSTATE = 1, 2, 3, 5
TO_HOST, TO_DEVICE = 0xA1, 0x21
READ_AHEAD = max(1, int(os.environ.get("DFU_READ_AHEAD", "32")))
OP_TIMEOUT = float(os.environ.get("DFU_OP_TIMEOUT", "15"))
FLASH_END = 16 * 1024 * 1024        # never read ahead past the radio's 16 MB SPI flash
ERR_IO, ERR_NO_DEVICE, ERR_TIMEOUT, ERR_PIPE = -1, -4, -7, -9   # libusb error codes dmrconf understands

# TyT identify string (DFU "a2 01" + UPLOAD block 0) -> qdmr radio keys it matches
IDENT_MODELS = {"DR780": ("md380",), "MD390": ("md390",), "MD-UV380": ("uv380", "rt3s"),
                "MD-UV390": ("uv390", "uv380", "rt3s"), "2017": ("md2017",), "DM-1701": ("dm1701",)}
DFU_MODELS = ("md380", "md390", "uv380", "uv390", "rt3s", "md2017", "dm1701")


def is_dfu_model(model: str | None) -> bool:
    return (model or "").lower() in DFU_MODELS


def _dfu_dir() -> str:
    d = os.environ.get("DFU_SOCKET_DIR") or os.path.join(tempfile.gettempdir(), "stentor-dfu")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


class DfuSession:
    """One user's WebUSB link. Owned by bridge._Port like a _PtySession (same takeover/cleanup rules)."""

    transport = "browser"
    kind = "dfu"

    def __init__(self, owner, ws, client: str | None):
        self.p, self.ws = owner, ws
        self.dir = tempfile.mkdtemp(prefix="u-", dir=_dfu_dir())        # 0700, private to this session
        self.path = os.path.join(self.dir, "s")
        self.link = "dfu:" + self.path
        self.server: asyncio.AbstractServer | None = None
        self.waiters: dict[int, asyncio.Future] = {}
        self.seq = 0
        self.xlock = asyncio.Lock()                  # one page operation at a time
        self.ahead: dict[int, tuple] = {}            # block number -> ((st, data), (st, status bytes))
        self.ahead_len = 0
        self.last_block = None
        self.status_next = None                      # GETSTATUS answer already fetched for dmrconf's next request
        self.closed = False
        owner.state.update({"active": True, "since": time.time(), "client": client, "to_radio": 0, "from_radio": 0,
                            "pty": self.link, "retries": 0, "verify_failures": 0, "transparent": False,
                            "transport": "browser", "run": None, "dfu": True})
        owner.trace.clear()
        owner.events.append({"t": round(time.time(), 3), "kind": "session", "pty": self.link, "transport": "browser-dfu"})
        owner.session = self

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, path=self.path)
        os.chmod(self.path, 0o600)

    # ------------------------------------------------------------------ page side
    def on_result(self, msg: dict) -> None:
        fut = self.waiters.pop(int(msg.get("id") or 0), None)
        if fut is not None and not fut.done():
            fut.set_result(msg.get("res") or [])

    async def _exec(self, ops: list[dict], timeout: float | None = None) -> list[tuple[int, bytes]]:
        """Run `ops` on the radio through the page, in order, stopping after the first failure.
        Returns [(status, data), …] — possibly fewer than `ops`."""
        async with self.xlock:
            if self.closed:
                return [(ERR_NO_DEVICE, b"")]
            self.seq += 1
            mid = self.seq
            fut = asyncio.get_running_loop().create_future()
            self.waiters[mid] = fut
            out = sum(len(bytes.fromhex(o.get("hex", ""))) for o in ops)
            self.p.state["to_radio"] += out + 8 * len(ops)
            try:
                await self.ws.send_json({"type": "dfu", "id": mid, "ops": ops})
                res = await asyncio.wait_for(fut, timeout or (OP_TIMEOUT + 0.05 * len(ops)))
            except asyncio.TimeoutError:
                self.waiters.pop(mid, None)
                self.p.events.append({"t": round(time.time(), 3), "kind": "dfu-timeout", "ops": len(ops)})
                return [(ERR_TIMEOUT, b"")]
            except Exception as exc:  # noqa: BLE001 — the WebSocket went away
                self.waiters.pop(mid, None)
                self.p.events.append({"t": round(time.time(), 3), "kind": "dfu-send", "error": str(exc)[:120]})
                return [(ERR_NO_DEVICE, b"")]
        parsed = []
        for r in res[:len(ops)]:
            try:
                st = int(r.get("st"))
                data = bytes.fromhex(r.get("hex") or "")
            except (TypeError, ValueError, AttributeError):
                st, data = ERR_IO, b""
            self.p.state["from_radio"] += len(data)
            parsed.append((st, data))
            if st < 0:
                break
        return parsed or [(ERR_IO, b"")]

    # ------------------------------------------------------------------ dmrconf side
    def _run(self) -> dict:
        if self.p.state.get("run") is None:
            self.p.state["run"] = {"started": round(time.time(), 3), "reads": 0, "writes": 0, "skipped": 0,
                                   "retries": 0, "diff_write": False, "ended": False, "dfu": True, "radio": None}
        return self.p.state["run"]

    def _end_run(self, ok: bool) -> None:
        run = self.p.state.get("run")
        if run:
            run["ended"] = True
            run["seconds"] = round(time.time() - run["started"], 1)
            self.p.state["last_run"] = run
            if ok:
                from . import bridge
                bridge._remember_size(run)
        self.p.state["run"] = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._run()
        self.ahead.clear()
        self.status_next = None
        self.last_block = None
        ok = True
        try:
            while True:
                try:
                    hdr = await reader.readexactly(9)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                if hdr[0] != ord("C"):
                    ok = False
                    break
                rt, rq, val, idx, ln = hdr[1], hdr[2], *struct.unpack("<HHH", hdr[3:9])
                data = await reader.readexactly(ln) if not (rt & 0x80) and ln else b""
                st, ans = await self._handle(rt, rq, val, idx, ln, data)
                if st < 0:
                    ok = False
                body = ans[:max(0, st)] if (rt & 0x80) and st > 0 else b""
                self.p.trace.append((time.time(), ">", hdr[:9] + data[:16]))
                self.p.trace.append((time.time(), "<", struct.pack("<i", st) + body[:16]))
                writer.write(struct.pack("<i", st) + body)
                await writer.drain()
        except Exception as exc:  # noqa: BLE001
            ok = False
            self.p.events.append({"t": round(time.time(), 3), "kind": "dfu-serve", "error": f"{type(exc).__name__}: {exc}"[:160]})
        finally:
            self._end_run(ok)
            self.ahead.clear()
            self.status_next = None
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _op(rt: int, rq: int, val: int, idx: int, ln: int, data: bytes = b"") -> dict:
        o = {"rt": rt, "rq": rq, "v": val, "i": idx, "n": ln}
        if data:
            o["hex"] = data.hex()
        return o

    def _status_op(self, idx: int) -> dict:
        return self._op(TO_HOST, GETSTATUS, 0, idx, 6)

    async def _handle(self, rt: int, rq: int, val: int, idx: int, ln: int, data: bytes) -> tuple[int, bytes]:
        run = self._run()
        # the GETSTATUS that qdmr always sends right after UPLOAD/DNLOAD was already run together with it
        if rt == TO_HOST and rq == GETSTATUS and self.status_next is not None:
            ans, self.status_next = self.status_next, None
            return ans
        self.status_next = None

        if rt == TO_HOST and rq == UPLOAD and val >= 2:                 # a codeplug block
            hit = self.ahead.pop(val, None) if ln == self.ahead_len else None
            if hit is None:
                sequential = self.last_block is not None and val == self.last_block + 1
                n = READ_AHEAD if sequential else 1
                end_block = (FLASH_END // max(1, ln)) + 2
                n = max(1, min(n, end_block - val))
                ops = []
                for k in range(n):
                    ops += [self._op(TO_HOST, UPLOAD, val + k, idx, ln), self._status_op(idx)]
                res = await self._exec(ops)
                self.ahead.clear()
                self.ahead_len = ln
                for k in range(n):
                    if 2 * k + 1 < len(res) and res[2 * k][0] >= 0 and res[2 * k + 1][0] >= 0:
                        self.ahead[val + k] = (res[2 * k], res[2 * k + 1])
                    else:
                        break
                hit = self.ahead.pop(val, None)
                if hit is None:                                         # the first read itself failed
                    self.ahead.clear()
                    if len(res) >= 2 and res[0][0] >= 0:                # read ok, status failed
                        self.status_next = res[1]
                    return res[0]
            self.last_block = val
            run["reads"] += 1
            self.status_next = hit[1]
            return hit[0]

        self.ahead.clear()
        self.last_block = None
        if rt == TO_HOST and rq == UPLOAD:                             # identify (block 0) and other small uploads
            res = await self._exec([self._op(rt, rq, val, idx, ln), self._status_op(idx)])
            if res[0][0] >= 0:
                if len(res) > 1:
                    self.status_next = res[1]
                if val == 0:
                    ident = res[0][1].split(b"\x00", 1)[0].decode("ascii", "replace").strip()
                    if ident and len(ident) <= 32:
                        run["radio"] = ident
            return res[0]
        if rt == TO_DEVICE and rq == DNLOAD:
            res = await self._exec([self._op(rt, rq, val, idx, ln, data), self._status_op(idx)])
            if res[0][0] >= 0:
                if len(res) > 1:
                    self.status_next = res[1]
                if val >= 2:
                    run["writes"] += 1
            return res[0]
        return (await self._exec([self._op(rt, rq, val, idx, ln, data)]))[0]

    # ------------------------------------------------------------------ teardown
    def radio_bytes(self, data: bytes) -> None:      # a DFU page never sends raw bytes
        pass

    def close(self) -> None:
        self.closed = True
        for fut in self.waiters.values():
            if not fut.done():
                fut.set_result([{"st": ERR_NO_DEVICE}])
        self.waiters.clear()
        if self.server is not None:
            self.server.close()
        shutil.rmtree(self.dir, ignore_errors=True)
        if self.p.session is self:
            self.p.session = None
            self.p.state.update({"active": False, "pty": None, "client": None, "transport": None, "run": None,
                                 "dfu": False})


def ident_matches(model: str | None, ident: str | None) -> bool | None:
    """True/False when a TyT identify string fits the selected model; None when unknown."""
    if not ident or not model:
        return None
    keys = IDENT_MODELS.get(ident.strip())
    return None if keys is None else (model.lower() in keys)
