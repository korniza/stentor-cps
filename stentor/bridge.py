# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Radio relay: a protocol-aware proxy between dmrconf (on a pseudo-terminal) and the radio.

Two transports feed it:
  * the browser bridge - the page opens the radio's USB port with Web Serial (Chrome/Edge) and streams raw bytes
    over a WebSocket (``serial_ws``); while it is up ``radio.bridge_link()`` returns its pty. TyT-family (DFU)
    radios use WebUSB instead (``dfubridge.py``, experimental);
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

from . import config, radio
from . import local as ctx

# ring buffer of the last frames in both directions, for debugging byte loss: (t, dir, bytes)
#   ">" host (dmrconf) -> radio,  "<" radio -> host,  "!" browser notices,  "x" dropped/retried,  "=" skipped write
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
# Pipelined reads: dmrconf reads 16 bytes per command and waits for each answer, so through the browser every block
# costs a full network + USB round trip (a D578UV read took > 40 min). When dmrconf asks for block A, the relay sends
# the reads for A, A+16 … A+16·(N-1) in one go (N·6 = 48 bytes — one USB packet), keeps the verified answers and
# serves dmrconf's next requests from them. Any bad answer → that session falls back to one block at a time.
READ_PIPELINE = max(1, int(os.environ.get("BRIDGE_READ_PIPELINE", "8")))
PIPE_TAIL = 0.4             # after the first answer, how long to wait for the rest of a batch
# Browser-side batches (0.4.65): radios that ignore queued commands (AT-D578UV V111) still answer in ~1 ms over USB —
# the cost is the ~50 ms WebSocket round trip per block. A page that says {"type":"hello","batch":1} gets
# {"type":"rb","addr":A,"n":N} instead: it does the N reads one after the other locally and returns all answers in
# one message; the relay verifies each like any other answer.
BROWSER_BATCH = max(1, int(os.environ.get("BRIDGE_BROWSER_BATCH", "64")))
# 0.4.76: the browser batch doubles (up to BROWSER_BATCH_MAX) while dmrconf uses every block read ahead, and goes
# back to BROWSER_BATCH as soon as it doesn't. One WebSocket round trip (~50 ms) costs as much as ~40 USB reads.
BROWSER_BATCH_MAX = max(BROWSER_BATCH, int(os.environ.get("BRIDGE_BROWSER_BATCH_MAX", "256")))

# Browser-side write batches (0.4.68): dmrconf's 16-byte writes are ACKed by the relay at once and sent to the page
# in groups ({"type":"wb","hex":…,"n":N}); the page writes them one by one, waiting for each ACK from the radio, and
# returns N status bytes (0x06 = written). A block that fails is retried normally; if it still fails, the next command
# from dmrconf (the next write, or END) gets a NAK, so the write stops with an error and END is never sent — the same
# outcome as a failed write without batching. At most WRITE_BATCH blocks are ACKed ahead of the radio.
WRITE_BATCH = max(1, int(os.environ.get("BRIDGE_WRITE_BATCH", "16")))
NAK = b"\x15"
# dmrconf gives up on a command after 4 s and then sends END on its way out — which makes an AnyTone commit a
# half-written session (it erases each touched sector at END). So when dmrconf had to wait longer than this for an
# answer after writes began, the session is marked failed and END is refused (NAK) — the radio keeps its old content.
HOST_WAIT_GUARD = float(os.environ.get("BRIDGE_HOST_WAIT_GUARD", "3.0"))
WRITE_FLUSH_BUDGET = 2.0    # a batch whose retries take longer than this stops the write (see HOST_WAIT_GUARD)


def _new_state() -> dict:
    return {"active": False, "since": None, "client": None, "to_radio": 0, "from_radio": 0, "pty": None,
            "retries": 0, "verify_failures": 0, "transparent": False, "transport": None,
            "run": None, "last_run": None, "cache_blocks": 0, "phase": None, "phase_since": None,
            "browser_batch": False}


class _Port:
    """Everything that belongs to the radio connection: state, block cache, overlay, lock and pty."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.state = _new_state()
        self.trace: collections.deque = collections.deque(maxlen=4000)
        self.events: collections.deque = collections.deque(maxlen=500)   # retries, timeouts, slow frames
        self.cache = _BlockCache(self)
        self.overlay: dict = {"reads": [], "writes": {}, "read_result": {}, "write_result": None}
        self.bridge_link: str | None = None          # pty of the browser bridge while it is up
        self.session = None                           # the live _PtySession, if any
        self.device: dict | None = None              # the browser's USB device + user agent (from "hello")
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock


_ports: dict[str, "_Port"] = {}
LOCAL = "_local"


def port(key: str | None = None) -> "_Port":
    """The radio port (one per server)."""
    k = key or LOCAL
    p = _ports.get(k)
    if p is None:
        p = _ports[k] = _Port(k)
    return p


def lock() -> asyncio.Lock:
    """Serialises programming sessions on the radio."""
    return port().lock


def link() -> str | None:
    return port().bridge_link


PHASES = ("read", "backup", "reboot", "write", "fm", "contacts")


def set_phase(phase: str | None) -> None:
    """What the current user's radio operation is doing now (shown by the UI while a job runs)."""
    st = port().state
    st["phase"], st["phase_since"] = phase, (round(time.time(), 1) if phase else None)


# How many blocks a full read / write of a radio model took last time (any user): the progress bar's 100 %.
# DATA_DIR/relay-sizes.json {"D578UV": {"read": 15053, "write": 15455}}; first-time defaults below.
_SIZE_DEFAULTS = {"D578UV": {"read": 15053, "write": 15455}, "D878UV2": {"read": 10561, "write": 10907},
                  "D878UV": {"read": 10561, "write": 10907}, "D868UVE": {"read": 9000, "write": 9300},
                  # TyT family over the DFU bridge: 1 KB blocks; qdmr reads the whole codeplug before it writes it,
                  # so a write counts reads + writes (0x3e000 + 0x90000 bytes = 824 blocks; MD-380/390: 248)
                  "DM-1701": {"read": 824, "write": 1648}, "MD-UV390": {"read": 824, "write": 1648},
                  "MD-UV380": {"read": 824, "write": 1648}, "2017": {"read": 824, "write": 1648},
                  "MD390": {"read": 248, "write": 496}, "DR780": {"read": 248, "write": 496}}
_sizes: dict | None = None


def _sizes_path():
    return config.DATA_DIR / "relay-sizes.json"


def _load_sizes() -> dict:
    global _sizes
    if _sizes is None:
        try:
            import json
            _sizes = json.loads(_sizes_path().read_text())
        except Exception:  # noqa: BLE001
            _sizes = {}
    return _sizes


def _remember_size(run: dict) -> None:
    """After a complete session: remember its size (reads for a read, writes for a write) for that radio model."""
    radio = (run.get("radio") or "").strip()
    if not radio:
        return
    kind, n = ("write", run.get("writes") or 0) if run.get("writes") else ("read", run.get("reads") or 0)
    if run.get("dfu") and kind == "write":            # TyT: the write session reads everything first
        n += run.get("reads") or 0
    if n < (200 if run.get("dfu") else 500):          # a detect or a tiny overlay session: not a full read/write
        return
    sizes = _load_sizes()
    sizes.setdefault(radio, {})[kind] = int(n)
    try:
        import json
        _sizes_path().write_text(json.dumps(sizes))
    except Exception:  # noqa: BLE001
        pass


def expected_blocks(radio: str | None, kind: str) -> int | None:
    radio = (radio or "").strip()
    return (_load_sizes().get(radio) or {}).get(kind) or (_SIZE_DEFAULTS.get(radio) or {}).get(kind)


def progress(key: str = LOCAL) -> dict:
    """Phase + live counters of the port, for the job poller; `pct` (0–99) and `eta_s` when the radio's usual
    size is known (see _remember_size)."""
    p = _ports.get(key)
    if p is None:
        return {}
    st, run = p.state, p.state.get("run") or {}
    out = {"phase": st.get("phase"), "phase_since": st.get("phase_since"), "bridge": bool(p.bridge_link),
           "reads": run.get("reads", 0), "writes": run.get("writes", 0), "retries": run.get("retries", 0),
           "from_radio": st.get("from_radio", 0), "radio": run.get("radio")}
    writing = bool(run.get("writes")) or st.get("phase") == "write"
    done, kind = (run.get("writes", 0), "write") if writing else (run.get("reads", 0), "read")
    if writing and run.get("dfu"):
        done += run.get("reads", 0)
    total = expected_blocks(run.get("radio"), kind)
    if total and run.get("started") and done:
        out["pct"] = max(0, min(99, int(100 * done / total)))
        el = time.time() - run["started"]
        if done > (40 if run.get("dfu") else 200) and el > 2:
            out["eta_s"] = max(0, int((total - done) * el / done))
    return out


def status() -> dict:
    p = port()
    out = {k: v for k, v in p.state.items()}
    out["bridge"] = bool(p.bridge_link)            # a browser bridge is attached and usable for Read/Write
    out["diff_write"] = DIFF_WRITE
    out["cache_blocks"] = len(p.cache.blocks)
    out["cache_age"] = round(time.time() - p.cache.t) if p.cache.blocks else None
    return out


def trace(last: int = 200) -> list[dict]:
    items = list(port().trace)[-last:]
    return [{"t": round(t, 4), "dir": d, "n": len(b), "hex": b.hex(" ")} for t, d, b in items]


def events() -> list[dict]:
    return list(port().events)


def device() -> dict | None:
    """The current user's last bridge device (USB VID:PID + browser), or the server USB."""
    p = port()
    if p.device:
        return dict(p.device)
    t = p.state.get("transport")
    return {"transport": t} if t else None


def last_run() -> dict | None:
    """Statistics of the most recent dmrconf session (PROGRAM..END) seen by the current user's relay."""
    lr = port().state["last_run"]
    return dict(lr) if lr else None


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

    def __init__(self, owner: "_Port") -> None:
        self.p = owner
        self.blocks: dict[int, bytes] = {}
        self.radio_id = b""
        self.t = 0.0

    def ensure(self, radio_id: bytes) -> None:
        if radio_id != self.radio_id or (self.blocks and time.time() - self.t > CACHE_TTL):
            self.blocks.clear()
            self.radio_id = radio_id
            self.p.events.append({"t": round(time.time(), 3), "kind": "cache", "action": "reset",
                            "radio": radio_id[1:8].decode(errors="replace")})

    def invalidate(self, why: str) -> None:
        if self.blocks:
            self.p.events.append({"t": round(time.time(), 3), "kind": "cache", "action": "invalidate", "why": why,
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


# Side-channel ("overlay") work the relay does inside the next programming session on behalf of the server:
# planned reads run right after the radio identified itself, planned writes go out just before END (so they win
# over whatever dmrconf wrote to the same addresses). Used for memory qdmr has no model for (FM broadcast list).
# Planned per port by the code that runs the session.


def plan_reads(addrs) -> None:
    ov = port().overlay
    ov["reads"] = sorted(set(int(a) for a in addrs))
    ov["read_result"] = {}


def plan_writes(blocks: dict[int, bytes | None]) -> None:
    """Blocks to write before END. A value of None means "keep": the block is rewritten with what the radio
    returned for it earlier in the session (plan the read too) - needed because the radio erases the sector."""
    bad = [a for a, d in blocks.items() if (d is not None and len(d) != 16) or a % 16]
    if bad:
        raise ValueError(f"overlay blocks must be 16 bytes at 16-byte aligned addresses: {bad[:3]}")
    ov = port().overlay
    ov["writes"] = dict(blocks)
    ov["write_result"] = None


def take_overlay() -> dict:
    """Collect and clear overlay results: {reads: {addr: 16 bytes}, writes: {sent, skipped, failed} | None}."""
    ov = port().overlay
    out = {"reads": dict(ov["read_result"]), "writes": ov["write_result"]}
    ov.update({"reads": [], "writes": {}, "read_result": {}, "write_result": None})
    return out


def _new_run() -> dict:
    return {"started": round(time.time(), 3), "reads": 0, "writes": 0, "skipped": 0, "retries": 0,
            "diff_write": DIFF_WRITE, "ended": False}


class _Relay:
    """One pty session. `send` delivers bytes to the radio (WebSocket or serial fd)."""

    def __init__(self, owner: "_Port", send, master_fd: int):
        self.p = owner
        self._send, self.master = send, master_fd
        self.host_buf = bytearray()                   # bytes from dmrconf not yet framed
        self.radio_q: asyncio.Queue[bytes] = asyncio.Queue()
        self.radio_buf = bytearray()
        self.cmd_q: asyncio.Queue[bytes] = asyncio.Queue()
        self.cmd_t: collections.deque = collections.deque()   # arrival time of each framed command from dmrconf
        self.transparent = False
        self.dirty = False                            # writes sent to the radio and no END yet
        self.prefetch: dict[int, bytes] = {}          # verified answers read ahead: addr -> 24-byte response
        self.pipe = READ_PIPELINE                     # batch size for this session (1 = off)
        self.send_ctl = None                          # async (dict) -> None: control message to the browser
        self.browser_batch = False                    # the page can do reads locally ("hello": batch)
        self.batch_fail = 0                           # consecutive failed browser batches
        self.rb_n = BROWSER_BATCH                     # current browser read batch size (adaptive)
        self.rb_used = 0                              # read-ahead hits from the latest batch
        self.rb_from = -1                             # first address of the latest batch
        self.write_batch = False                      # the page can do batched writes ("hello": wbatch)
        self.wpend: list[bytes] = []                  # writes ACKed to dmrconf, not yet sent to the radio
        self.wfail: str | None = None                 # a batched write failed: refuse everything until the session ends

    # --- host (pty) side
    def host_bytes(self, data: bytes) -> None:
        self.p.trace.append((time.time(), ">", data))
        if self.transparent:
            self.cmd_q.put_nowait(data)
            return
        self.host_buf += data
        while self.host_buf:
            spec = _CMDS.get(self.host_buf[0])
            if spec is None:                          # unknown command: give up framing for this session
                self.transparent = True
                self.p.state["transparent"] = True
                self.p.cache.invalidate("transparent session")
                self.cmd_q.put_nowait(bytes(self.host_buf))
                self.host_buf.clear()
                return
            n = spec[0]
            if len(self.host_buf) < n:
                return
            self.cmd_q.put_nowait(bytes(self.host_buf[:n]))
            self.cmd_t.append(time.monotonic())
            del self.host_buf[:n]

    # --- radio side
    def radio_bytes(self, data: bytes) -> None:
        self.p.trace.append((time.time(), "<", data))
        self.p.state["from_radio"] += len(data)
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
            self.p.trace.append((time.time(), "x", dropped))

    async def _exchange(self, cmd: bytes, resp_len: int) -> bytes:
        t0 = time.monotonic()
        await self._send(cmd)
        self.p.state["to_radio"] += len(cmd)
        resp = await self._recv(resp_len)
        dt = time.monotonic() - t0
        if dt > SLOW_FRAME:
            self.p.events.append({"t": round(time.time(), 3), "kind": "slow", "cmd": cmd[:6].hex(), "ms": round(dt * 1000), "got": len(resp)})
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
        if self.p.state["run"] is None:
            self.p.state["run"] = _new_run()
        return self.p.state["run"]

    def _end_run(self) -> None:
        run = self.p.state["run"]
        if run:
            run["ended"] = True
            run["seconds"] = round(time.time() - run["started"], 1)
            self.p.state["last_run"] = run
            if not self.wfail:
                _remember_size(run)
        self.p.state["run"] = None

    async def _robust(self, cmd: bytes, run: dict) -> tuple[bytes, str | None]:
        """Send one framed command, verify the answer, retry on corruption. Returns (response, reason-or-None)."""
        spec = _CMDS[cmd[0]]
        resp, reason = b"", None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                self.p.state["retries"] += 1
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
                        self.p.state["verify_failures"] += 1
            else:
                reason = None if len(resp) == spec[1] else f"short ({len(resp)})"
            if reason is None:
                break
            self.p.trace.append((time.time(), "x", f"attempt {attempt}: {reason}".encode()))
            self.p.events.append({"t": round(time.time(), 3), "kind": "retry", "cmd": cmd[:6].hex(), "attempt": attempt,
                            "reason": reason, "resp": resp[:8].hex()})
            if cmd[0] not in (ord("R"), ord("W")):
                break                              # control commands: pass whatever came back
        return resp, reason

    @staticmethod
    def _wframe(addr: int, data: bytes) -> bytes:
        body = b"W" + addr.to_bytes(4, "big") + b"\x10" + data
        return body + bytes([sum(body[1:]) & 0xFF, 0x06])

    async def _overlay_reads(self, run: dict) -> None:
        for addr in self.p.overlay["reads"]:
            cmd = b"R" + addr.to_bytes(4, "big") + b"\x10"
            resp, reason = await self._robust(cmd, run)
            if reason is None:
                self.p.overlay["read_result"][addr] = resp[6:22]
                self.p.cache.note_read(addr, resp[6:22])
                run["reads"] += 1
        self.p.overlay["reads"] = []

    async def _overlay_writes(self, run: dict) -> None:
        res = {"sent": 0, "skipped": 0, "failed": []}
        for addr, data in sorted(self.p.overlay["writes"].items()):
            keep = data is None
            if keep:                                   # KEEP: rewrite what the radio holds (sector-erase safe)
                data = self.p.overlay["read_result"].get(addr) or self.p.cache.blocks.get(addr)
                if data is None:
                    res["failed"].append({"addr": f"{addr:08x}", "reason": "keep: block not read"})
                    continue
            if DIFF_WRITE and not keep and self.p.cache.same(addr, data):
                res["skipped"] += 1
                continue
            resp, reason = await self._robust(self._wframe(addr, data), run)
            if reason is None:
                res["sent"] += 1
                run["writes"] += 1
                self.dirty = True
                self.p.cache.note_write(addr, data)
            else:
                res["failed"].append({"addr": f"{addr:08x}", "reason": reason})
                self.p.cache.invalidate("unconfirmed overlay write")
        self.p.overlay["writes"] = {}
        self.p.overlay["write_result"] = res
        if res["sent"] or res["failed"]:
            self.p.events.append({"t": round(time.time(), 3), "kind": "overlay", **{k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}})

    async def _read_batch(self, cmd: bytes, run: dict) -> tuple[bytes, str | None] | None:
        """Pipelined read for `cmd` (see READ_PIPELINE). Returns (response, None) when the first answer is good —
        the following good answers go to self.prefetch — or None to let the caller do a normal exchange."""
        addr = int.from_bytes(cmd[1:5], "big")
        local = bool(self.browser_batch and self.send_ctl)
        if local:
            # Grow only while dmrconf reads straight through (every block of the previous batch was used); otherwise
            # back to BROWSER_BATCH. Never drop the read-ahead on a miss: dmrconf interleaves regions (the D578 reads
            # 4 blocks at 0x009C1E80, 4 at 0x009C3E80, then 0x009C1EC0 ...), so older answers are still hits later.
            if run.get("batches"):
                if self.rb_used >= self.rb_n - 1:
                    self.rb_n = min(BROWSER_BATCH_MAX, self.rb_n * 2)
                else:
                    self.rb_n = BROWSER_BATCH
            self.rb_used = 0
            self.rb_from = addr
            run["batches"] = run.get("batches", 0) + 1
        n = self.rb_n if local else self.pipe
        cmds = [b"R" + (addr + 16 * i).to_bytes(4, "big") + b"\x10" for i in range(n)]
        t0 = time.monotonic()
        if local:
            await self.send_ctl({"type": "rb", "addr": addr, "n": n})
        else:
            await self._send(b"".join(cmds))
        self.p.state["to_radio"] += 6 * len(cmds)
        first = await self._recv(24)
        if _check_read(cmds[0], first) is not None:
            self.radio_buf[:0] = first                # let _drain see it in the trace
            await self._drain(0.3)
            reason = _check_read(cmds[0], first)
            if local:                                 # a Web Serial hiccup: retry this block normally, keep batching
                self.batch_fail += 1
                if self.batch_fail >= 3:
                    self.browser_batch = False
                    self.p.events.append({"t": round(time.time(), 3), "kind": "pipeline", "action": "browser batch off",
                                          "reason": reason, "cmd": cmd[:6].hex()})
            else:
                self.pipe = 1
                self.p.events.append({"t": round(time.time(), 3), "kind": "pipeline", "action": "off",
                                      "reason": reason, "cmd": cmd[:6].hex()})
            return None
        self.batch_fail = 0
        rest = await self._recv(24 * (len(cmds) - 1), timeout=PIPE_TAIL)
        good = 1
        for i, c in enumerate(cmds[1:]):
            chunk = rest[24 * i: 24 * i + 24]
            if _check_read(c, chunk) is not None:
                if chunk and not local:               # a corrupt answer (not just a late one): stop pipelining
                    self.pipe = 1
                    self.p.events.append({"t": round(time.time(), 3), "kind": "pipeline", "action": "off",
                                          "reason": _check_read(c, chunk), "cmd": c.hex()})
                await self._drain(0.3)                # late answers must not reach the next exchange
                break
            self.prefetch[int.from_bytes(c[1:5], "big")] = chunk
            good += 1
        if not local and good == 1 and len(cmds) > 1 and self.pipe > 1:   # the radio ignores queued commands
            self.pipe = 1
            self.p.events.append({"t": round(time.time(), 3), "kind": "pipeline", "action": "off",
                                  "reason": "no answers to the queued reads", "cmd": cmd[:6].hex()})
        run["pipelined"] = run.get("pipelined", 0) + good - 1
        dt = time.monotonic() - t0
        if dt > SLOW_FRAME * 2:
            self.p.events.append({"t": round(time.time(), 3), "kind": "slow", "cmd": cmd[:6].hex(), "ms": round(dt * 1000),
                                  "batch": len(cmds), "got": good})
        return first, None

    def _batch_writes_ok(self) -> bool:
        return bool(self.write_batch and self.send_ctl and not self.transparent and not VERIFY_WRITES and WRITE_BATCH > 1)

    async def _flush_writes(self, run: dict) -> None:
        """Send the pending writes to the page in one message, check the N status bytes, retry failures normally."""
        frames, self.wpend = self.wpend, []
        if not frames or self.wfail:
            return
        t0 = time.monotonic()
        await self.send_ctl({"type": "wb", "hex": b"".join(frames).hex(), "n": len(frames)})
        self.p.state["to_radio"] += 24 * len(frames)
        acks = await self._recv(len(frames), timeout=RESP_TIMEOUT + 0.25 * len(frames))
        self.dirty = True
        retry = False
        for i, f in enumerate(frames):
            if not retry and i < len(acks) and acks[i] == 0x06:
                self._bookkeep(f, b"\x06", run)
                continue
            if not retry:                              # first failure: anything after it goes one by one
                retry = True
                await self._drain(0.3)
                self.p.events.append({"t": round(time.time(), 3), "kind": "retry", "cmd": f[:6].hex(),
                                      "reason": f"batched write status {acks[i:i + 1].hex() or 'missing'}"})
            if time.monotonic() - t0 > WRITE_FLUSH_BUDGET:
                reason = "the radio is too slow to acknowledge writes"
            else:
                resp, reason = await self._robust(f, run)
            if reason is None:
                self._bookkeep(f, resp, run)
            else:
                self.wfail = reason
                self.p.cache.invalidate("unconfirmed write")
                self.p.events.append({"t": round(time.time(), 3), "kind": "write_failed", "cmd": f[:6].hex(), "reason": reason})
                return
        run["batched_writes"] = run.get("batched_writes", 0) + len(frames)
        dt = time.monotonic() - t0
        if dt > 1.0 + 0.05 * len(frames):
            self.p.events.append({"t": round(time.time(), 3), "kind": "slow", "cmd": "wb", "ms": round(dt * 1000), "batch": len(frames)})

    async def _reply(self, cmd: bytes, data: bytes, t_in: float) -> None:
        """Answer dmrconf; if it waited so long that it has probably given up, stop the session before END."""
        waited = time.monotonic() - t_in
        if (self.dirty or self.wpend) and waited > HOST_WAIT_GUARD and cmd[:1] != b"E" and not self.wfail:
            self.wfail = f"dmrconf waited {waited:.1f} s for an answer — write stopped before END"
            self.p.cache.invalidate("host timeout during write")
            self.p.events.append({"t": round(time.time(), 3), "kind": "write_failed", "cmd": cmd[:6].hex(),
                                  "reason": self.wfail})
        await self._to_host(data)

    async def run(self) -> None:
        while True:
            cmd = await self.cmd_q.get()
            t_in = self.cmd_t.popleft() if (self.cmd_t and not self.transparent) else time.monotonic()
            if not self.transparent and (self.wpend or self.wfail):
                run = self._run_stats()
                if cmd[0] == ord("W") and self.wfail is None and self._batch_writes_ok():
                    pass                               # handled below with the other writes
                else:
                    await self._flush_writes(run)
                    if self.wfail:                     # refuse: dmrconf reports the error, END never reaches the radio
                        await self._reply(cmd, NAK * _CMDS.get(cmd[0], (0, 1))[1], t_in)
                        continue
            if not self.transparent and cmd[0] == ord("W") and self._batch_writes_ok() and not (
                    DIFF_WRITE and self.p.cache.same(int.from_bytes(cmd[1:5], "big"), cmd[6:22])):
                run = self._run_stats()
                self.prefetch.clear()
                self.wpend.append(cmd)
                if len(self.wpend) >= WRITE_BATCH:
                    await self._flush_writes(run)
                await self._reply(cmd, NAK if self.wfail else b"\x06", t_in)
                continue
            if not self.transparent and cmd[0] == ord("R"):
                hit = self.prefetch.pop(int.from_bytes(cmd[1:5], "big"), None)
                if hit is not None:                   # answered from the read-ahead
                    a = int.from_bytes(cmd[1:5], "big")
                    if self.rb_from <= a < self.rb_from + 16 * self.rb_n:
                        self.rb_used += 1             # only hits from the latest batch count for growing
                    run = self._run_stats()
                    self._bookkeep(cmd, hit, run)
                    await self._reply(cmd, hit, t_in)
                    continue
                if (self.pipe > 1 or (self.browser_batch and self.send_ctl)) and not self.p.overlay["reads"]:
                    run = self._run_stats()
                    got = await self._read_batch(cmd, run)
                    if got is not None:
                        self._bookkeep(cmd, got[0], run)
                        await self._reply(cmd, got[0], t_in)
                        continue
            elif self.prefetch:
                self.prefetch.clear()                 # anything but a read: read-ahead may be stale
            if self.transparent:
                await self._send(cmd)
                self.p.state["to_radio"] += len(cmd)
                continue
            if cmd[0] == ord("P"):
                self._end_run()
                self.p.state["run"] = _new_run()
            run = self._run_stats()
            if cmd[0] == ord("W") and DIFF_WRITE and self.p.cache.same(int.from_bytes(cmd[1:5], "big"), cmd[6:22]):
                run["skipped"] += 1
                self.p.trace.append((time.time(), "=", cmd[:6]))
                await self._reply(cmd, b"\x06", t_in)
                continue
            if cmd[0] == ord("E") and self.wfail:     # the session failed: never let the radio commit it
                await self._to_host(NAK)
                continue
            if cmd[0] == ord("E") and self.p.overlay["writes"]:
                await self._overlay_writes(run)
            resp, reason = await self._robust(cmd, run)
            if reason is None:
                self._bookkeep(cmd, resp, run)
                if cmd[0] == 0x02 and self.p.overlay["reads"]:
                    await self._overlay_reads(run)
            elif cmd[0] == ord("W"):
                self.dirty = True                      # a write may or may not have landed: forget what we know
                self.p.cache.invalidate("unconfirmed write")
                self.wfail = self.wfail or f"write not acknowledged ({reason})"   # dmrconf will quit: refuse its END
            await self._reply(cmd, resp, t_in)         # good, or the last bad one so dmrconf reports it

    def _bookkeep(self, cmd: bytes, resp: bytes, run: dict) -> None:
        c = cmd[0]
        if c == 0x02 and resp[:1] == b"I":
            self.p.cache.ensure(resp)
            run["radio"] = resp[1:8].decode(errors="replace").strip("\x00")
            run["firmware"] = resp[9:15].decode(errors="replace").strip("\x00 ") or None   # 'I' model(7) band(1) version(6) ACK
        elif c == ord("R"):
            run["reads"] += 1
            self.p.cache.note_read(int.from_bytes(cmd[1:5], "big"), resp[6:22])
        elif c == ord("W"):
            run["writes"] += 1
            self.dirty = True
            self.p.cache.note_write(int.from_bytes(cmd[1:5], "big"), cmd[6:22])
        elif c == ord("E"):
            self.dirty = False                         # radio commits on END
            self._end_run()

    def close(self) -> None:
        if self.dirty:
            self.p.cache.invalidate("session ended without END")
        self._end_run()


async def _pump_transparent(relay: _Relay) -> None:
    """In transparent mode radio bytes go straight to the pty."""
    while True:
        data = await relay.radio_q.get()
        await relay._to_host(data)


class _PtySession:
    """A pty with a relay behind it. Common to both transports."""

    def __init__(self, owner: "_Port", send, transport: str, client: str | None):
        self.p = owner
        self.master, self.slave = pty.openpty()
        tty.setraw(self.master)
        tty.setraw(self.slave)
        self.link = os.ttyname(self.slave)
        self.loop = asyncio.get_running_loop()
        owner.state.update({"active": True, "since": time.time(), "client": client, "to_radio": 0, "from_radio": 0,
                            "pty": self.link, "retries": 0, "verify_failures": 0, "transparent": False,
                            "transport": transport, "run": None})
        owner.trace.clear()
        owner.events.append({"t": round(time.time(), 3), "kind": "session", "pty": self.link, "transport": transport})
        owner.session = self
        self.relay = _Relay(owner, send, self.master)
        self.pump: asyncio.Task | None = None
        self.transport = transport
        self.ws = None
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
        if self.p.session is self:                  # a newer session of the same user may already own the port
            self.p.session = None
            self.p.state.update({"active": False, "pty": None, "client": None, "transport": None, "run": None})


# ---------------------------------------------------------------- transport 1: browser (WebSocket)
async def serial_ws(ws: WebSocket) -> None:
    if ws.headers.get("x-shack") != "1" and ws.query_params.get("shack") != "1":
        await ws.close(code=4403)
        return
    p = port()
    if p.state["active"]:
        old = p.session
        if old is None or old.transport != "browser" or p.lock.locked():
            await ws.close(code=4409)  # the radio is busy (a read/write is running) or on the server USB
            return
        # the port re-opened (new tab, reconnect after a dropped link): the newer page takes over
        p.events.append({"t": round(time.time(), 3), "kind": "takeover", "old": old.link})
        if old.ws is not None:
            try:
                await old.ws.close(code=4410)
            except Exception:  # noqa: BLE001
                pass
        old.close()
        p.bridge_link = None
    await ws.accept()
    if ws.query_params.get("dfu") == "1":            # TyT family: WebUSB control transfers (dfubridge.py)
        await _dfu_ws(ws, p)
        return
    session = _PtySession(p, ws.send_bytes, "browser", ws.client.host if ws.client else None)
    session.ws = ws
    session.relay.send_ctl = ws.send_json
    p.bridge_link = session.link
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
                p.trace.append((time.time(), "!", msg["text"].encode()))
                if '"hello"' in msg["text"]:
                    try:
                        hello = __import__("json").loads(msg["text"])
                    except ValueError:
                        hello = {}
                    vid, pid = int(hello.get("vid") or 0), int(hello.get("pid") or 0)
                    p.device = {"usb": f"{vid:04X}:{pid:04X}" if vid else None, "ua": str(hello.get("ua") or "")[:200],
                                "transport": "browser"}
                    session.relay.browser_batch = bool(hello.get("batch"))
                    session.relay.write_batch = bool(hello.get("wbatch"))
                    p.state["browser_batch"] = session.relay.browser_batch
                    p.state["write_batch"] = session.relay.write_batch
            elif msg.get("text") == "ping":
                await ws.send_json({"type": "pong", **{k: p.state[k] for k in ("to_radio", "from_radio", "retries")},
                                    "phase": p.state.get("phase")})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        # Only the session that still owns the port may clear the link. Comparing pty names is not enough: after a
        # takeover the kernel often gives the new page the SAME /dev/pts/N, and the old page's cleanup then cleared the
        # new page's bridge (Read/Write: "no radio connected" while ⚡ showed connected) — fixed in 0.4.38.
        if p.session is session or p.session is None:
            if p.bridge_link == session.link:
                p.bridge_link = None
        session.close()


async def _dfu_ws(ws: WebSocket, p: "_Port") -> None:
    """The WebUSB page of a TyT-family radio: dmrconf talks to dfubridge.DfuSession over a unix socket."""
    import json
    from .dfubridge import DfuSession
    session = DfuSession(p, ws, ws.client.host if ws.client else None)
    try:
        await session.start()
    except OSError as exc:
        session.close()
        await ws.close(code=1011, reason=str(exc)[:100])
        return
    p.bridge_link = session.link
    try:
        await ws.send_json({"type": "ready", "dfu": True})
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text") or ""
            if text == "ping":
                await ws.send_json({"type": "pong", **{k: p.state[k] for k in ("to_radio", "from_radio", "retries")},
                                    "phase": p.state.get("phase")})
                continue
            if not text.startswith("{"):
                continue
            try:
                m = json.loads(text)
            except ValueError:
                continue
            if m.get("type") == "dfur":
                session.on_result(m)
            elif m.get("type") == "hello":
                vid, pid = int(m.get("vid") or 0), int(m.get("pid") or 0)
                p.device = {"usb": f"{vid:04X}:{pid:04X}" if vid else None, "ua": str(m.get("ua") or "")[:200],
                            "transport": "browser", "dfu": True,
                            "product": str(m.get("product") or "")[:60] or None}
                p.trace.append((time.time(), "!", text[:300].encode()))
            else:
                p.trace.append((time.time(), "!", text[:300].encode()))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if p.session is session or p.session is None:
            if p.bridge_link == session.link:
                p.bridge_link = None
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
    p = port()
    if p.state["active"]:
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

    session = _PtySession(p, send, "usb", device)

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
    p = port()
    if p.bridge_link:
        link = p.bridge_link
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
