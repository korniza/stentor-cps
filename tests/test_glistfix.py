# SPDX-License-Identifier: AGPL-3.0-or-later
"""D578UV group-list slot work-around (stentor/glistfix.py): layout over the slots the radio still has enabled,
spare lists only in the written file, stripped again after a read, preview warning when lists will not fit.
Radio mocked. Run:
  DATA_DIR=/tmp/t CALLSIGN=SV4ABC DMR_ID=2024999 HOTSPOT_KIND=none RADIO_MODEL=d578uv python tests/test_glistfix.py
"""
import asyncio
import os
import re
import sys

os.environ.setdefault("RADIO_MODEL", "d578uv")
from stentor import codeplug as cp, local, glistfix as g, radio  # noqa: E402
from stentor import server as s  # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{str(extra)[:300]}]" if extra and not cond else ""))


# ---- plan
p = g.plan(1, [2, 3, 4, 5, 6])                         # a real D578UV: bitmap 0x7C
check("0x7C, 1 list: spares in slots 0,1, list in 2, slots 3..6 kept", p["layout"] == "PPRPPPP" and p["lost"] == 0, p)
p = g.plan(3, list(range(250)))
check("all slots free: lists first, RESERVE spares after", p["layout"] == "RRR" + "P" * g.RESERVE, p)
p = g.plan(3, [2, 3])
check("not enough slots: lost counted", p["placed"] == 2 and p["lost"] == 1 and p["layout"] == "PPRR", p)
p = g.plan(2, [])
check("no slot at all: nothing to lay out", p["layout"] == "" and p["lost"] == 2, p)
p = g.plan(0, [0, 1, 2])
check("no lists: enabled slots kept with spares", p["layout"] == "PPP", p)
p = g.plan(2, [0, 1, 5])
check("trailing spare only up to an enabled slot", p["layout"] == "RRPPPP" and p["capacity"] == 3, p)

# ---- decode
blk = {g.BITMAP_ADDR: bytes([0x7C]) + b"\0" * 15, g.BITMAP_ADDR + 16: b"\0" * 16}
check("decode 0x7C -> slots 2..6", g.decode(blk) == [2, 3, 4, 5, 6], g.decode(blk))
check("decode with a missing block -> None", g.decode({g.BITMAP_ADDR: b"\xff" * 16}) is None)
check("only the D578 is affected", g.affected("d578uv") and not g.affected("d878uv2"))

written = {}


async def fake_read(label="", also_binary=False):
    g.save_state([2, 3, 4, 5, 6], "D578UV")             # what the overlay read stores
    return {"ok": True, "yaml": "qdmr/2026/pre-write.yaml"}


async def fake_write(path, init_codeplug=False, update_clock=True, fm_list=None):
    written["text"] = open(path).read()
    written["path"] = str(path)
    return {"ok": True, "output": ""}


async def fake_ready(was_bridge):
    return {"ok": True}


async def fake_check(path):
    return {"ok": True}

radio.read_codeplug, radio.write_codeplug, radio.wait_ready, radio.encode_check = fake_read, fake_write, fake_ready, fake_check


def lists_of(text):
    doc = cp._yaml.load(text)
    return [str(x["name"]) for x in doc.get("groupLists") or []]


async def main():
    s.codeplug_new("current", "SV4ABC", 2024999)
    doc = cp.load("current")
    cont = cp.upsert(doc, "contacts", {"name": "Greece", "ring": False, "type": "GroupCall", "number": 202}, "dmr")
    cp.upsert(doc, "groupLists", {"name": "RX Repeaters", "contacts": [cont]})
    cp.save("current", doc)
    cp.flush_all()
    before = cp.path_for("current").read_text()
    real = lists_of(before)

    g.save_state([2, 3, 4, 5, 6], "D578UV")
    pv = await s.radio_write("current")
    check("preview reports the layout", (pv.get("glist") or {}).get("known") and pv["glist"]["lost"] == 0, pv.get("glist"))
    r = await s.radio_write("current", confirm_token=pv["confirm_token"])
    check("write ok", r.get("ok"), r)
    wl = lists_of(written["text"])
    check("written file: spares before the real lists in locked slots 0,1",
          wl[:2] == ["STNTR SPARE 1", "STNTR SPARE 2"] and wl[2:2 + len(real)] == real, wl)
    check("written file keeps slots 3..6 alive", len(wl) == 7, wl)
    check("result says the lists were placed", (r.get("glist") or {}).get("applied"), r.get("glist"))
    check("user's codeplug unchanged", cp.path_for("current").read_text() == before)
    check("last-read has no spare lists", "STNTR SPARE" not in cp.path_for("last-read").read_text())
    check("temporary slots file removed", not os.path.exists(written["path"]))

    # read back from the radio: spare lists show up, Stentor strips the unreferenced ones
    rb = local.workspace() / "rb.yaml"
    rb.write_text(written["text"])
    n = g.strip_pads(rb)
    check("strip_pads removes the spare lists after a read", n == 6 and lists_of(rb.read_text()) == real, (n, lists_of(rb.read_text())))

    # not enough slots: preview warns
    g.save_state([2], "D578UV")
    doc = cp.load("current")
    cp.upsert(doc, "groupLists", {"name": "Second", "contacts": [cont]})
    cp.save("current", doc)
    cp.flush_all()
    pv = await s.radio_write("current")
    codes = [i.get("code") for i in (pv.get("lint") or {}).get("issues", [])]
    check("preview warns when lists do not fit", "d578_glist_slots" in codes and pv["glist"]["lost"] == len(real), (codes, pv.get("glist")))

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
