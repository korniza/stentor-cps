# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""A run that lost the browser bridge is reported as bridge_lost (not "decode"), a snapshot with the last
address is kept; DMRCONF_TRACE switches the verbose trace on.
Run: DATA_DIR=<empty> python tests/test_bridge_lost.py"""
import asyncio
import collections
import time
from types import SimpleNamespace

from stentor import config, radio, server as srv

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print("PASS" if cond else "FAIL", name, "" if cond else str(extra)[:300])


out = ("\x1b[...]\n                  ] 55%\nERROR: the browser bridge closed (radio port lost) - dmrconf stopped "
       "Cannot decode codeplug")
check("bridge loss wins over 'decode'", srv.fail_reason(out) == "bridge_lost", srv.fail_reason(out))
check("other reasons unchanged", srv.fail_reason("No response from device: Timeout") == "timeout")
check("last progress", radio._last_progress("] 12%\n ] 34%\n] 55%") == "55" and radio._last_progress("x") is None)

# snapshot: fake port with a trace of R commands and answers
from stentor import bridge  # noqa: E402
now = time.time()
trace = collections.deque(maxlen=4000)
for i in range(10):
    addr = 0x02140000 + i * 0x10
    trace.append((now - 1 + i * 0.01, ">", b"R" + addr.to_bytes(4, "big") + b"\x10"))
    trace.append((now - 1 + i * 0.01 + 0.005, "<", b"W" + addr.to_bytes(4, "big") + b"\x10" + b"\x00" * 18))
fake = SimpleNamespace(trace=trace, events=[], device={"usb": "28E9:018A", "transport": "browser"},
                       session=SimpleNamespace(relay=SimpleNamespace(rb_from=0x02140000)))
real_port = bridge.port
bridge.port = lambda: fake
try:
    radio._save_bridge_snapshot("d878uv2", ["read", "--radio", "d878uv2"], "the browser bridge closed (radio port lost) - dmrconf stopped", out)
finally:
    bridge.port = real_port
ev = fake.events[-1] if fake.events else {}
check("event added", ev.get("kind") == "bridge_fail" and ev.get("progress") == "55", ev)
check("last read address", (ev.get("last_read") or {}).get("addr") == "0x02140090", ev)
check("batch start", ev.get("batch_from") == "0x02140000", ev)
files = sorted((config.DATA_DIR / "traces").glob("*-bridge.log"))
check("snapshot file written", files and "last read" in files[-1].read_text(), files)


import os  # noqa: E402
os.environ.pop("DMRCONF_TRACE", None)
check("trace off by default", not radio.trace_on())
os.environ["DMRCONF_TRACE"] = "1"
check("DMRCONF_TRACE=1 -> trace on", radio.trace_on())
os.environ.pop("DMRCONF_TRACE", None)
html = (srv.Path(srv.__file__).parent / "static" / "shack.html").read_text()
check("page: bridge_lost text el+en", html.count("bridge_lost:") == 2 and "{pct}" in html)
print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
