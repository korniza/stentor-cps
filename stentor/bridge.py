# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Radio relay: a protocol-aware proxy between dmrconf (on a pseudo-terminal) and the radio.

Two transports feed it:
  * the browser bridge - the page opens the radio's USB port with Web Serial (Chrome/Edge) and streams raw bytes
    over a WebSocket (``serial_ws``); one bridge at a time, and while it is up ``radio.bridge_device`` points at the pty;
  * the server's own USB port (``serial_relay``) - the same relay in front of /dev/ttyACMx, so both paths behave alike.

The relay knows the AnyTone programming protocol (D868/D878/D578/BTECH 6X2): every host command is a fixed-length
frame with a fixed-length response, so it holds each command, verifies the radio's answer (address, size, checksum,
ACK) and silently re-issues the command when the answer is corrupt. Web Serial on Windows was observed to mangle
roughly one chunk in 1,500 (bytes dropped / stale bytes), which otherwise kills a whole read.

Differential write: every block the radio returns during a read is remembered (per radio identity). When dmrconf
later writes a block whose content equals what the radio already holds, the relay ACKs it locally instead of
sending it - the safety-backup read that precedes every write makes the cache fresh, so a small change takes
seconds instead of minutes. Blocks that were never read (e.g. the callsign DB) always go to the radio. The cache is
dropped when a write session ends without END (abort), when the radio identity changes, or after CACHE_TTL.

Unknown commands switch the session to transparent byte pumping (other radio families)."""
import asyncio
import collections
import os
import pty
import termios
import time
import tty
from contextlib import asynccontextmanager

from starlette.websockets import WebSocket, WebSocketDisconnect

from . import radio

_state: dict = {"active": False, "since": None, "client": None, "to_radio": 0, "from_radio": 0, "pty": None,
                "retries": 0, "verify_failures": 0, "transparent": False, "transport": None,
                "run": None, "last_run": None, "cache_blocks": 0}
# ring buffer of the last frames in both directions, for debugging byte loss: (t, dir, bytes)
#   ">" host (dmrconf) -> radio,  "<" radio -> host,  "!" browser notices,  "x" dropped/retried,  "=" skipped write
_trace: collections.deque = collections.deque(maxlen=4000)
# Read-back-after-write is OFF by default: the AnyTone answers reads from the old content until the session ends,
# so every block would "mismatch". The radio ACKs each block after checking its checksum; verify afterwards with
# radio_verify (full read + compare) if wanted.
VERIFY_WRITES = os.environ.get("BRIDGE_VERIFY_WRITES", "0") == "1"
# DANGER - off by default since 0.4.1: AnyTone radios erase the whole flash sector at END and program only the
# blocks received in the session, so a skipped (identical) block comes back as 0xFF. Observed on a D878UVII Plus:
# general + boot settings wiped -> Chinese UI and a power-on password. Only enable for radios known to do
# read-modify-write per block.
DIFF_WRITE = os.environ.get("DIFF_WRITE", "0") == "1"
USB_RELAY = os.environ.get("USB_RELAY", "1") != "0"
CACHE_TTL = float(os.environ.get("DIFF_WRITE_TTL", "1800"))
MAX_ATTEMPTS = 4
RESP_TIMEOUT = 2.5          # our dmrconf build waits 4 s per read (patched from 1 s), so a retry fits inside it
SLOW_FRAME = 0.25
_events: collections.deque = collections.deque(maxlen=500)   # retries, timeouts, slow frames: survive the trace


def status() -> dict:
    out = {k: v for k, v in _state.items()}
    out["diff_write"] = DIFF_WRITE
    out["cache_blocks"] = len(_cache.blocks)
    out["cache_age"] = round(time.time() - _cache.t) if _cache.blocks else None
    return out


def trace(last: int = 200) -> list[dict]:
    items = list(_trace)[-last:]
    return [{"t": round(t, 4), "dir": d, "n": len(b), "hex": b.hex(" ")} for t, d, b in items]


def events() -> list[dict]:
    return list(_events)


def last_run() -> dict | None:
    """Statistics of the most recent dmrconf session (PROGRAM..END) seen by the relay."""
    return dict(_state["last_run"]) if _state["last_run"] else None


# ---------------------------------------------------------------- AnyTone frame knowledge
# first byte of a host command -> (command length, response length)
_CMDS = {ord("P"): (7, 3),      # "PROGRAM" -> "QX\x06"
         0x02: (1, 16),         # identify -> 'I' + model(7) + bands + version(6) + 0x06
         ord("R"): (6, 24),     # 'R' addr(4) 0x10 -> 'W' addr(4) 0x10 data(16) sum 0x06
         ord("W"): (24, 1),     # 'W' addr(4) 0x10 data(16) sum 0x06 -> 0x06
         ord("E"): (3, 1)}      # "END" -> 0x06


def _check_read(req: bytes, resp: bytes) -> str | None:
    """Return None when `resp` is a valid answer to read request `req`, else a short reason."""
    if len(resp) != 24:
        return f"short ({len(resp)})"
    if resp[0] != ord("W"):
        return f"cmd {resp[0]:02x}"
    if resp[1:5] != req[1:5]:
        return f"addr {resp[1:5].hex()} != {req[1:5].hex()}"
    if resp[5] != 16:
        return f"size {resp[5]}"
    if (sum(resp[1:22]) & 0xFF) != resp[22]:
        return "checksum"
    if resp[23] != 0x06:
        return f"ack {resp[23]:02x}"
    return None


class _BlockCache:
    """What the radio holds, as far as we have seen it: 16-byte blocks by address."""

    def __init__(self) -> None:
        self.blocks: dict[int, bytes] = {}
        self.radio_id = b""
        self.t = 0.0

    def ensure(self, radio_id: bytes) -> None:
        if radio_id != self.radio_id or (self.blocks and time.time() - self.t > CACHE_TTL):
            self.blocks.clear()
            self.radio_id = radio_id
            _events.append({"t": round(time.time(), 3), "kind": "cache", "action": "reset",
                            "radio": radio_id[1:8].decode(errors="replace")})

    def invalidate(self, why: str) -> None:
        if self.blocks:
            _events.append({"t": round(time.time(), 3), "kind": "cache", "action": "invalidate", "why": why,
                            "blocks": len(self.blocks)})
        self.blocks.clear()

    def note_read(self, addr: int, data: bytes) -> None:
        self.blocks[addr] = data
        self.t = time.time()

    def note_write(self, addr: int, data: bytes) -> None:
        if addr in self.blocks:           # never grow the cache from writes (the callsign DB is tens of MB)
            self.blocks[addr] = data

    def same(self, addr: int, data: bytes) -> bool:
        return self.blocks.get(addr) == data


_cache = _BlockCache()

# Side-channel ("overlay") work the relay does inside the next programming session on behalf of the server:
# planned reads run right after the radio identified itself, planned writes go out just before END (so they win
# over whatever dmrconf wrote to the same addresses). Used for memory qdmr has no model for (FM broadcast list).
_overlay: dict = {"reads": [], "writes": {}, "read_result": {}, "write_result": None}


def plan_reads(addrs) -> None:
    _overlay["reads"] = sorted(set(int(a) for a in addrs))
    _overlay["read_result"] = {}


def plan_writes(blocks: dict[int, bytes | None]) -> None:
    """Blocks to write before END. A value of None means "keep": the block is rewritten with what the radio
    returned for it earlier in the session (plan the read too) - needed because the radio erases the sector."""
    bad = [a for a, d in blocks.items() if (d is not None and len(d) != 16) or a % 16]
    if bad:
        raise ValueError(f"overlay blocks must be 16 bytes at 16-byte aligned addresses: {bad[:3]}")
    _overlay["writes"] = dict(blocks)
    _overlay["write_result"] = None


def take_overlay() -> dict:
    """Collect and clear overlay results: {reads: {addr: 16 bytes}, writes: {sent, skipped, failed} | None}."""
    out = {"reads": dict(_overlay["read_result"]), "writes": _overlay["write_result"]}
    _overlay.update({"reads": [], "writes": {}, "read_result": {}, "write_result": None})
    return out


def _new_run() -> dict:
    return {"started": round(time.time(), 3), "reads": 0, "writes": 0, "skipped": 0, "retries": 0,
            "diff_write": DIFF_WRITE, "ended": False}


class _Relay:
    """One pty session. `send` delivers bytes to the radio (WebSocket or serial fd)."""

    def __init__(self, send, master_fd: int):
        self._send, self.master = send, master_fd
        self.host_buf = bytearray()                   # bytes from dmrconf not yet framed
        self.radio_q: asyncio.Queue[bytes] = asyncio.Queue()
        self.radio_buf = bytearray()
        self.cmd_q: asyncio.Queue[bytes] = asyncio.Queue()
        self.transparent = False
        self.dirty = False                            # writes sent to the radio and no END yet

    # --- host (pty) side
    def host_bytes(self, data: bytes) -> None:
        _trace.append((time.time(), ">", data))
        if self.transparent:
            self.cmd_q.put_nowait(data)
            return
        self.host_buf += data
        while self.host_buf:
            spec = _CMDS.get(self.host_buf[0])
            if spec is None:                          # unknown command: give up framing for this session
                self.transparent = True
                _state["transparent"] = True
                _cache.invalidate("transparent session")
                self.cmd_q.put_nowait(bytes(self.host_buf))
                self.host_buf.clear()
                return
            n = spec[0]
            if len(self.host_buf) < n:
                return
            self.cmd_q.put_nowait(bytes(self.host_buf[:n]))
            del self.host_buf[:n]

    # --- radio side
    def radio_bytes(self, data: bytes) -> None:
        _trace.append((time.time(), "<", data))
        _state["from_radio"] += len(data)
        self.radio_q.put_nowait(data)

    async def _recv(self, n: int, timeout: float = RESP_TIMEOUT) -> bytes:
        """Collect exactly n bytes from the radio (or fewer on timeout)."""
        deadline = time.monotonic() + timeout
        while len(self.radio_buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                self.radio_buf += await asyncio.wait_for(self.radio_q.get(), left)
            except asyncio.TimeoutError:
                break
        out = bytes(self.radio_buf[:n])
        del self.radio_buf[:n]
        return out

    async def _drain(self, settle: float = 0.03) -> None:
        """Throw away whatever the radio still sends after a corrupt exchange."""
        dropped = bytes(self.radio_buf)
        self.radio_buf.clear()
        while True:
            try:
                dropped += await asyncio.wait_for(self.radio_q.get(), settle)
            except asyncio.TimeoutError:
                break
        if dropped:
            _trace.append((time.time(), "x", dropped))

    async def _exchange(self, cmd: bytes, resp_len: int) -> bytes:
        t0 = time.monotonic()
        await self._send(cmd)
        _state["to_radio"] += len(cmd)
        resp = await self._recv(resp_len)
        dt = time.monotonic() - t0
        if dt > SLOW_FRAME:
            _events.append({"t": round(time.time(), 3), "kind": "slow", "cmd": cmd[:6].hex(), "ms": round(dt * 1000), "got": len(resp)})
        return resp

    async def _to_host(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.master, view)
            except BlockingIOError:
                await asyncio.sleep(0.005)
                continue
            view = view[n:]

    def _run_stats(self) -> dict:
        if _state["run"] is None:
            _state["run"] = _new_run()
        return _state["run"]

    def _end_run(self) -> None:
        run = _state["run"]
        if run:
            run["ended"] = True
            run["seconds"] = round(time.time() - run["started"], 1)
            _state["last_run"] = run
        _state["run"] = None

    async def _robust(self, cmd: bytes, run: dict) -> tuple[bytes, str | None]:
        """Send one framed command, verify the answer, retry on corruption. Returns (response, reason-or-None)."""
        spec = _CMDS[cmd[0]]
        resp, reason = b"", None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                _state["retries"] += 1
                run["retries"] += 1
                await self._drain()
            resp = await self._exchange(cmd, spec[1])
            if cmd[0] == ord("R"):
                reason = _check_read(cmd, resp)
            elif cmd[0] == ord("W"):
                reason = None if resp == b"\x06" else f"write ack {resp.hex()}"
                if reason is None and VERIFY_WRITES:
                    rb = await self._exchange(b"R" + cmd[1:6], 24)
                    if _check_read(b"R" + cmd[1:6], rb) is not None:
                        reason = "read-back corrupt"
                    elif rb[6:22] != cmd[6:22]:
                        reason = "read-back mismatch"
                        _state["verify_failures"] += 1
            else:
                reason = None if len(resp) == spec[1] else f"short ({len(resp)})"
            if reason is None:
                break
            _trace.append((time.time(), "x", f"attempt {attempt}: {reason}".encode()))
            _events.append({"t": round(time.time(), 3), "kind": "retry", "cmd": cmd[:6].hex(), "attempt": attempt,
                            "reason": reason, "resp": resp[:8].hex()})
            if cmd[0] not in (ord("R"), ord("W")):
                break                              # control commands: pass whatever came back
        return resp, reason

    @staticmethod
    def _wframe(addr: int, data: bytes) -> bytes:
        body = b"W" + addr.to_bytes(4, "big") + b"\x10" + data
        return body + bytes([sum(body[1:]) & 0xFF, 0x06])

    async def _overlay_reads(self, run: dict) -> None:
        for addr in _overlay["reads"]:
            cmd = b"R" + addr.to_bytes(4, "big") + b"\x10"
            resp, reason = await self._robust(cmd, run)
            if reason is None:
                _overlay["read_result"][addr] = resp[6:22]
                _cache.note_read(addr, resp[6:22])
                run["reads"] += 1
        _overlay["reads"] = []

    async def _overlay_writes(self, run: dict) -> None:
        res = {"sent": 0, "skipped": 0, "failed": []}
        for addr, data in sorted(_overlay["writes"].items()):
            keep = data is None
            if keep:                                   # KEEP: rewrite what the radio holds (sector-erase safe)
                data = _overlay["read_result"].get(addr) or _cache.blocks.get(addr)
                if data is None:
                    res["failed"].append({"addr": f"{addr:08x}", "reason": "keep: block not read"})
                    continue
            if DIFF_WRITE and not keep and _cache.same(addr, data):
                res["skipped"] += 1
                continue
            resp, reason = await self._robust(self._wframe(addr, data), run)
            if reason is None:
                res["sent"] += 1
                run["writes"] += 1
                self.dirty = True
                _cache.note_write(addr, data)
            else:
                res["failed"].append({"addr": f"{addr:08x}", "reason": reason})
                _cache.invalidate("unconfirmed overlay write")
        _overlay["writes"] = {}
        _overlay["write_result"] = res
        if res["sent"] or res["failed"]:
            _events.append({"t": round(time.time(), 3), "kind": "overlay", **{k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}})

    async def run(self) -> None:
        while True:
            cmd = await self.cmd_q.get()
            if self.transparent:
                await self._send(cmd)
                _state["to_radio"] += len(cmd)
                continue
            if cmd[0] == ord("P"):
                self._end_run()
                _state["run"] = _new_run()
            run = self._run_stats()
            if cmd[0] == ord("W") and DIFF_WRITE and _cache.same(int.from_bytes(cmd[1:5], "big"), cmd[6:22]):
                run["skipped"] += 1
                _trace.append((time.time(), "=", cmd[:6]))
                await self._to_host(b"\x06")
                continue
            if cmd[0] == ord("E") and _overlay["writes"]:
                await self._overlay_writes(run)
            resp, reason = await self._robust(cmd, run)
            if reason is None:
                self._bookkeep(cmd, resp, run)
                if cmd[0] == 0x02 and _overlay["reads"]:
                    await self._overlay_reads(run)
            elif cmd[0] == ord("W"):
                self.dirty = True                      # a write may or may not have landed: forget what we know
                _cache.invalidate("unconfirmed write")
            await self._to_host(resp)                  # good, or the last bad one so dmrconf reports it

    def _bookkeep(self, cmd: bytes, resp: bytes, run: dict) -> None:
        c = cmd[0]
        if c == 0x02 and resp[:1] == b"I":
            _cache.ensure(resp)
            run["radio"] = resp[1:8].decode(errors="replace").strip("\x00")
        elif c == ord("R"):
            run["reads"] += 1
            _cache.note_read(int.from_bytes(cmd[1:5], "big"), resp[6:22])
        elif c == ord("W"):
            run["writes"] += 1
            self.dirty = True
            _cache.note_write(int.from_bytes(cmd[1:5], "big"), cmd[6:22])
        elif c == ord("E"):
            self.dirty = False                         # radio commits on END
            self._end_run()

    def close(self) -> None:
        if self.dirty:
            _cache.invalidate("session ended without END")
        self._end_run()


async def _pump_transparent(relay: _Relay) -> None:
    """In transparent mode radio bytes go straight to the pty."""
    while True:
        data = await relay.radio_q.get()
        await relay._to_host(data)


class _PtySession:
    """A pty with a relay behind it. Common to both transports."""

    def __init__(self, send, transport: str, client: str | None):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.master)
        tty.setraw(self.slave)
        self.link = os.ttyname(self.slave)
        self.loop = asyncio.get_running_loop()
        _state.update({"active": True, "since": time.time(), "client": client, "to_radio": 0, "from_radio": 0,
                       "pty": self.link, "retries": 0, "verify_failures": 0, "transparent": False,
                       "transport": transport, "run": None})
        _trace.clear()
        _events.append({"t": round(time.time(), 3), "kind": "session", "pty": self.link, "transport": transport})
        self.relay = _Relay(send, self.master)
        self.pump: asyncio.Task | None = None
        os.set_blocking(self.master, False)
        self.loop.add_reader(self.master, self._on_master_readable)
        self.worker = asyncio.create_task(self.relay.run())

    def _on_master_readable(self) -> None:
        try:
            data = os.read(self.master, 4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if data:
            self.relay.host_bytes(data)

    def radio_bytes(self, data: bytes) -> None:
        self.relay.radio_bytes(data)
        if self.relay.transparent and self.pump is None:
            self.pump = asyncio.create_task(_pump_transparent(self.relay))

    def close(self) -> None:
        self.worker.cancel()
        if self.pump:
            self.pump.cancel()
        try:
            self.loop.remove_reader(self.master)
        except Exception:  # noqa: BLE001
            pass
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass
        self.relay.close()
        _state.update({"active": False, "pty": None, "client": None, "transport": None, "run": None})


# ---------------------------------------------------------------- transport 1: browser (WebSocket)
async def serial_ws(ws: WebSocket) -> None:
    if ws.headers.get("x-shack") != "1" and ws.query_params.get("shack") != "1":
        await ws.close(code=4403)
        return
    if _state["active"]:
        await ws.close(code=4409)  # already bridged
        return
    await ws.accept()
    session = _PtySession(ws.send_bytes, "browser", ws.client.host if ws.client else None)
    radio.bridge_device = session.link
    try:
        await ws.send_json({"type": "ready", "pty": session.link})
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data:
                session.radio_bytes(data)
            elif msg.get("text", "").startswith("{"):
                _trace.append((time.time(), "!", msg["text"].encode()))
            elif msg.get("text") == "ping":
                await ws.send_json({"type": "pong", **{k: _state[k] for k in ("to_radio", "from_radio", "retries")}})
    except WebSocketDisconnect:
        pass
    finally:
        radio.bridge_device = None
        session.close()


# ---------------------------------------------------------------- transport 2: server USB port
def _open_serial(device: str) -> int:
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = termios.B115200
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~termios.CRTSCTS
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
    except termios.error:
        pass                                           # USB CDC: baud/flags are decorative
    return fd


@asynccontextmanager
async def serial_relay(device: str):
    """Put the relay in front of a local serial device; yields the pty path dmrconf should open.
    Raises OSError when the device cannot be opened (busy, gone)."""
    if _state["active"]:
        raise OSError("relay busy (browser bridge active)")
    fd = _open_serial(device)
    loop = asyncio.get_running_loop()

    async def send(data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(fd, view)
            except BlockingIOError:
                await asyncio.sleep(0.002)
                continue
            view = view[n:]

    session = _PtySession(send, "usb", device)

    def on_readable() -> None:
        try:
            data = os.read(fd, 4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if data:
            session.radio_bytes(data)

    loop.add_reader(fd, on_readable)
    try:
        yield session.link
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:  # noqa: BLE001
            pass
        session.close()
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------- short sessions without dmrconf
async def direct_session(reads=(), writes: dict[int, bytes] | None = None, device: str | None = None,
                         timeout: float = 60.0) -> dict:
    """PROGRAM / identify / [planned reads] / [planned writes] / END on the current transport (the browser bridge if
    it is up, else the local USB port). The relay does the block work; this function only drives the session.
    The radio reboots afterwards like after any programming session."""
    plan_reads(reads)
    plan_writes(writes or {})
    cm = None
    if _state["active"] and _state["transport"] == "browser":
        link = _state["pty"]
    else:
        dev = device or (radio.serial_candidates()[:1] or [None])[0]
        if not dev:
            take_overlay()
            raise RuntimeError("no radio: neither a browser bridge nor a USB serial port")
        cm = serial_relay(dev)
        link = await cm.__aenter__()
    loop = asyncio.get_running_loop()
    fd = os.open(link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    q: asyncio.Queue[bytes] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(fd, 4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if data:
            q.put_nowait(data)

    async def xfer(cmd: bytes, n: int, t: float) -> bytes:
        os.write(fd, cmd)
        buf, deadline = b"", time.monotonic() + t
        while len(buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                buf += await asyncio.wait_for(q.get(), left)
            except asyncio.TimeoutError:
                break
        return buf

    loop.add_reader(fd, on_readable)
    t0 = time.monotonic()
    try:
        if await xfer(b"PROGRAM", 3, 6.0) != b"QX\x06":
            raise RuntimeError("radio did not enter programming mode (is it on and connected?)")
        ident = await xfer(b"\x02", 16, timeout)      # the relay runs the planned reads before answering
        if ident[:1] != b"I":
            raise RuntimeError(f"unexpected identify answer {ident.hex()}")
        end = await xfer(b"END", 1, timeout)          # the relay runs the planned writes before forwarding END
        if end != b"\x06":
            raise RuntimeError("END not acknowledged")
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:  # noqa: BLE001
            pass
        os.close(fd)
        if cm is not None:
            await cm.__aexit__(None, None, None)
    out = take_overlay()
    out["radio"] = ident[1:8].decode(errors="replace").strip("\x00")
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out
