# SPDX-License-Identifier: AGPL-3.0-or-later
"""radio_write sends exactly the previewed file, even when it is 'last-read' (which the pre-write safety backup
replaces), and leaves last-read = what was written. Radio mocked. Run:
  DATA_DIR=/tmp/t CALLSIGN=SV4ABC DMR_ID=2024999 HOTSPOT_KIND=none python tests/test_write_snapshot.py
"""
import asyncio
import sys

from stentor import codeplug as cp, local, fmradio, radio
from stentor import server as s

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{str(extra)[:300]}]" if extra and not cond else ""))


RADIO_FM = [{"mhz": 99.0, "name": "Old"}]
written = {}


async def fake_read(label="", also_binary=False):
    # what radio.read_codeplug does to the workspace: last-read (+ FM) := the radio's current content
    cp.path_for("last-read").write_text("# OLD-RADIO-CONTENT\n")
    cp.forget("last-read")
    fmradio.save_list("last-read", RADIO_FM)
    return {"ok": True, "yaml": "qdmr/2026/pre-write.yaml"}


async def fake_write(path, init_codeplug=False, update_clock=True, fm_list=None):
    written["text"] = open(path).read()
    written["fm"] = fm_list
    return {"ok": True, "output": ""}


async def fake_ready(was_bridge):
    return {"ok": True}


async def fake_check(path):
    return {"ok": True}

radio.read_codeplug, radio.write_codeplug, radio.wait_ready, radio.encode_check = fake_read, fake_write, fake_ready, fake_check


async def main():
    marker = "MARKER-8000202"
    s.codeplug_new("last-read", "SV4ABC", 2024999)
    cp.flush_all()
    cp.path_for("last-read").write_text(cp.path_for("last-read").read_text() + f"\n# {marker}\n")
    cp.forget("last-read")
    fmradio.save_list("last-read", [{"mhz": 101.5, "name": "Mine"}])

    pv = await s.radio_write("last-read")
    check("preview gives a token", pv.get("confirm_token"), pv)
    r = await s.radio_write("last-read", confirm_token=pv["confirm_token"])
    check("write ok", r.get("ok"), r)
    check("the radio got the previewed file, not the safety backup", marker in written.get("text", ""), written.get("text", "")[-200:])
    check("the radio got the previewed FM list", [x["mhz"] for x in written.get("fm") or []] == [101.5], written.get("fm"))
    check("last-read now = what was written", marker in cp.path_for("last-read").read_text())
    check("last-read FM = what was written", [x["mhz"] for x in fmradio.load_list("last-read")] == [101.5], fmradio.load_list("last-read"))
    check("snapshot cleaned up", not any((local.workspace() / ".write").glob("*.yaml")))
    names = [c["name"] for c in cp.list_workspace()]
    check("snapshot dir not listed as a codeplug", names == ["last-read"], names)
    r = s.codeplug_copy("last-read", "last-read", overwrite=True)
    check("copy onto itself is a no-op, not an error", r.get("ok") and r.get("unchanged"), r)
    # an ordinary codeplug: written as previewed, last-read follows it
    s.codeplug_new("current", "SV4ABC", 2024999)
    cp.flush_all()
    cp.path_for("current").write_text(cp.path_for("current").read_text() + "\n# CURRENT-MARK\n")
    cp.forget("current")
    pv = await s.radio_write("current")
    r = await s.radio_write("current", confirm_token=pv["confirm_token"])
    check("current: written as previewed, last-read follows", r.get("ok") and "CURRENT-MARK" in written["text"]
          and "CURRENT-MARK" in cp.path_for("last-read").read_text())
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
