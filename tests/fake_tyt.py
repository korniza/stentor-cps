# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake TyT-family radio (MD-UV390 / DM-1701 …) at the USB DFU control-transfer level, for the DFU bridge tests.

Implements what qdmr's TyTInterface uses (DfuSe on 0483:DF11): DNLOAD block 0 commands (0x91 0x01 enter programming,
0xA2 0x01 identify mode, 0x21 set address, 0x41 erase a 64 KB page, 0x91 0x05 reboot), DNLOAD/UPLOAD of data blocks
(address = base + (block - 2) * length), GETSTATUS / GETSTATE / CLRSTATUS / ABORT and the DFU state machine.

Flash is modelled as NOR flash: a write may only clear bits (old & new == new); writing a block that was not erased
is counted in `unerased_writes`, so a test notices when a write skips the erase."""
import threading

DNLOAD, UPLOAD, GETSTATUS, CLRSTATUS, GETSTATE, ABORT = 1, 2, 3, 4, 5, 6
IDLE, DNLOAD_SYNC, DNBUSY, DNLOAD_IDLE, UPLOAD_IDLE, ERROR = 2, 3, 4, 5, 9, 10
ERR_PIPE = -9


class FakeTyT:
    def __init__(self, ident: str = "DM-1701", size: int = 16 * 1024 * 1024):
        self.ident = ident.encode()
        self.mem = bytearray(b"\xff" * size)
        self.base = 0
        self.state = IDLE
        self.pending = None            # DNLOAD block 0 command, run on the next GETSTATUS (DfuSe)
        self.ident_mode = False
        self.programming = False
        self.unerased_writes = 0
        self.erased_pages = []
        self.reboots = 0
        self.log = []                  # (rq, value, length)
        self.lock = threading.Lock()

    def _status(self, state: int) -> bytes:
        return bytes([0, 0, 0, 0, state, 0])

    def ctrl(self, rt: int, rq: int, value: int, index: int, length: int, data: bytes = b"") -> tuple[int, bytes]:
        """One control transfer -> (status, data): status = bytes done or a negative libusb error."""
        with self.lock:
            self.log.append((rq, value, length))
            if rq == GETSTATE:
                st = self.state
                if st == DNBUSY:                      # busy is over by the time anyone asks again
                    self.state = st = DNLOAD_IDLE
                return 1, bytes([st])
            if rq == GETSTATUS:
                if self.state == DNLOAD_SYNC:
                    self._run_pending()
                    if self.state != ERROR:
                        self.state = DNBUSY
                return 6, self._status(self.state)
            if rq in (CLRSTATUS, ABORT):
                self.state = IDLE
                return 0, b""
            if rq == UPLOAD:
                if self.state == ERROR:
                    return ERR_PIPE, b""
                if value == 0:
                    if not self.ident_mode:
                        self.state = ERROR
                        return ERR_PIPE, b""
                    self.ident_mode = False
                    self.state = UPLOAD_IDLE
                    return length, (self.ident + b"\x00" * length)[:length]
                if value < 2:
                    self.state = ERROR
                    return ERR_PIPE, b""
                addr = self.base + (value - 2) * length
                if addr + length > len(self.mem):
                    self.state = ERROR
                    return ERR_PIPE, b""
                self.state = UPLOAD_IDLE
                return length, bytes(self.mem[addr:addr + length])
            if rq == DNLOAD:
                if self.state == ERROR:
                    return ERR_PIPE, b""
                data = bytes(data[:length])
                if value == 0:
                    self.pending = data
                    self.state = DNLOAD_SYNC
                    if not data:                         # leave DFU: the radio restarts
                        self.reboots += 1
                        self.pending = None
                        self.state = IDLE
                    return len(data), b""
                addr = self.base + (value - 2) * len(data)
                if addr + len(data) > len(self.mem):
                    self.state = ERROR
                    return ERR_PIPE, b""
                old = self.mem[addr:addr + len(data)]
                if any((o & n) != n for o, n in zip(old, data)):
                    self.unerased_writes += 1
                self.mem[addr:addr + len(data)] = bytes(o & n for o, n in zip(old, data))
                self.state = DNLOAD_SYNC
                self.pending = None
                return len(data), b""
            return ERR_PIPE, b""

    def _run_pending(self) -> None:
        cmd, self.pending = self.pending, None
        if not cmd:
            return
        if cmd[:2] == b"\x91\x01":
            self.programming = True
        elif cmd[:2] == b"\xa2\x01":
            self.ident_mode = True
        elif cmd[:2] == b"\x91\x05":
            self.reboots += 1
        elif cmd[0] == 0x21 and len(cmd) == 5:
            self.base = int.from_bytes(cmd[1:5], "little")
        elif cmd[0] == 0x41 and len(cmd) == 5:
            a = int.from_bytes(cmd[1:5], "little") & ~0xFFFF
            self.mem[a:a + 0x10000] = b"\xff" * 0x10000
            self.erased_pages.append(a)
        else:
            self.state = ERROR
