# SPDX-License-Identifier: AGPL-3.0-or-later
"""Channel scan list reaches the radio (0.4.95): qdmr 0.15 names the channel's scan-list reference `scanListRef`;
Stentor keeps `scanList` and translates at the dmrconf boundary (codeplug.qdmr_yaml_text / radio._qdmr_input) and
on load (codeplug.from_qdmr). The encode part runs when dmrconf is installed and checks the scan-list index byte
(channel offset 0x1b) of the AnyTone binary. Run:
  DATA_DIR=/tmp/t CALLSIGN=SV4ABC DMR_ID=2024999 HOTSPOT_KIND=none RADIO_MODEL=d878uv2 python tests/test_scanlist_ref.py
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("RADIO_MODEL", "d878uv2")
from stentor import codeplug as cp, radio  # noqa: E402

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{str(extra)[:400]}]" if extra and not cond else ""))


SRC = """---
version: 0.15.1
settings:
  scanList: notachannel
channels:
  - fm:
      id: ch1
      name: "A1"
      rxFrequency: 152.25 MHz
      scanList: scan1
      anytone:
        scanList: deep
  - dmr:
      id: ch2
      name: "B2"
      scanListRef: scan9
      scanList: scan2
      power: High
  - fm:
      id: ch3
      name: "C3"
      scanList: ~
      scanListRef: scan1
  - fm:
      id: ch4
      name: "D4"
      scanListRef: scan1
  - fm:
      id: ch5
      name: "E5"
      scanList: null
scanLists:
  - id: scan1
    name: "S1"
    channels: [ch1]
  - id: scan2
    name: "S2"
    channels: [ch2]
"""
out = cp.qdmr_yaml_text(SRC)
check("ch1 scanList -> scanListRef (same place)", "      rxFrequency: 152.25 MHz\n      scanListRef: scan1\n      anytone:" in out, out)
check("nested key below the body indent untouched", "        scanList: deep" in out, out)
check("ch2 stale scanListRef replaced by scanList's value", "scanListRef: scan9" not in out and "      scanListRef: scan2\n      power: High" in out, out)
check("ch3 null scanList drops both (cleared in Stentor)", 'name: "C3"\n  - fm:' in out, out)
check("ch4 radio-read style (scanListRef only) kept", 'name: "D4"\n      scanListRef: scan1' in out, out)
check("ch5 null scanList dropped", 'name: "E5"\nscanLists:' in out, out)
check("no channel scanList: key left", not any(l.strip().startswith("scanList:") and l.startswith("      ") and not l.startswith("        ") for l in out.splitlines()), out)
check("settings / scanLists sections untouched", "  scanList: notachannel" in out and out.endswith(SRC.split("scanLists:\n", 1)[1]), out)
check("idempotent", cp.qdmr_yaml_text(out) == out)
check("no scanList at all -> same text", cp.qdmr_yaml_text("channels:\n  - fm:\n      id: ch1\n") == "channels:\n  - fm:\n      id: ch1\n")

FLOW = "channels: [fm: {id: ch1, name: A, scanList: scan1}, fm: {id: ch2, name: B, scanList: ~, power: Low}, fm: {id: ch3, scanListRef: scan9, scanList: scan2}, fm: {scanList: null, id: ch4}]\nscanLists:\n  - {id: scan1, name: S1, channels: [ch1]}\n"
fo = cp.qdmr_yaml_text(FLOW)
check("flow: scanList -> scanListRef", "{id: ch1, name: A, scanListRef: scan1}" in fo, fo)
check("flow: null scanList removed", "{id: ch2, name: B, power: Low}" in fo and "{id: ch4}" in fo, fo)
check("flow: stale scanListRef replaced", "{id: ch3, scanListRef: scan2}" in fo, fo)
check("flow: scanLists line untouched + idempotent", fo.endswith("  - {id: scan1, name: S1, channels: [ch1]}\n") and cp.qdmr_yaml_text(fo) == fo, fo)
BF = "channels:\n  - fm: {id: ch1, scanList: scan1}\n  - fm:\n      id: ch2\n      rxTone: {ctcss: 88.5 Hz}\n      scanList: scan1\n"
bo = cp.qdmr_yaml_text(BF)
check("mixed block/flow items", "  - fm: {id: ch1, scanListRef: scan1}\n" in bo and "      rxTone: {ctcss: 88.5 Hz}\n      scanListRef: scan1\n" in bo, bo)

doc = cp._yaml.load(SRC)
n = cp.from_qdmr(doc)
bodies = {next(iter(c.values()))["id"]: next(iter(c.values())) for c in doc["channels"]}
check("from_qdmr counts the bodies with scanListRef", n == 3, n)
check("from_qdmr: existing scanList wins", bodies["ch2"]["scanList"] == "scan2" and "scanListRef" not in bodies["ch2"], dict(bodies["ch2"]))
check("from_qdmr: ref fills a missing/null scanList", bodies["ch4"]["scanList"] == "scan1" and bodies["ch3"]["scanList"] == "scan1")
check("from_qdmr: no scanListRef left in memory", not any("scanListRef" in b for b in bodies.values()))

# _qdmr_input: argument swap only for write/verify/encode --yaml
tmpd = Path(tempfile.mkdtemp())
src = tmpd / "x.yaml"
src.write_text(SRC)
a = ["write", "--yaml", "--device", "/dev/pts/9", "--update-device-clock", str(src)]
t = radio._qdmr_input(a)
check("write: yaml after a flag is swapped for a temp copy", t is not None and a[-1] == str(t) and t.exists(), a)
check("temp copy is the translated text", t and t.read_text() == out)
t and t.unlink()
a = ["read", "--yaml", "--device", "x", str(src)]
check("read: untouched", radio._qdmr_input(a) is None and a[-1] == str(src))
plain = tmpd / "p.yaml"
plain.write_text("channels:\n  - fm:\n      id: ch1\n")
a = ["encode", "--yaml", "--radio", "d878uv2", str(plain), str(tmpd / "o.dfu")]
check("encode without scanList: no copy", radio._qdmr_input(a) is None and a[4] == str(plain))

# ---- real encode (AnyTone channel byte 0x1b = scan list index, 0xff = none)
if shutil.which("dmrconf"):
    d = cp.new_minimal("SV4ABC", 2024999)
    cp.upsert(d, "channels", {"name": "SCANTEST1", "rxFrequency": 145.5, "txFrequency": 145.5, "rxOnly": True}, kind="fm")
    cp.upsert(d, "channels", {"name": "SCANTEST2", "rxFrequency": 145.525, "txFrequency": 145.525}, kind="fm")
    cp.upsert(d, "channels", {"name": "SCANTEST3", "rxFrequency": 145.55, "txFrequency": 145.55}, kind="fm")
    cp.upsert(d, "zones", {"name": "Z", "A": ["ch1", "ch2", "ch3"]})
    cp.upsert(d, "scanLists", {"name": "S1", "channels": ["ch1", "ch2"]})
    cp.upsert(d, "scanLists", {"name": "S2", "channels": ["ch3"]})
    cp.upsert(d, "channels", {"id": "ch1", "scanList": "scan1"})
    cp.upsert(d, "channels", {"id": "ch3", "scanList": "scan2"})
    y = tmpd / "enc.yaml"
    y.write_text(cp.dump_str(d))

    def idx(dfu: Path, name: bytes):
        b = dfu.read_bytes()
        i = b.find(name + b"\0")
        return None if i < 0 else b[i - 0x23 + 0x1b]

    raw = tmpd / "raw.dfu"
    subprocess.run(["dmrconf", "encode", "--yaml", "--radio", "d878uv2", str(y), str(raw)], capture_output=True, timeout=120)
    r = asyncio.run(radio.run_dmrconf(["encode", "--yaml", "--radio", "d878uv2", str(y), str(tmpd / "fixed.dfu")], timeout=120))
    fixed = tmpd / "fixed.dfu"
    check("dmrconf encode through run_dmrconf succeeded", r[0] == 0 and fixed.exists(), r)
    if fixed.exists():
        check("ch1 -> scan list 0", idx(fixed, b"SCANTEST1") == 0, idx(fixed, b"SCANTEST1"))
        check("ch2 (no scan list) -> 0xff", idx(fixed, b"SCANTEST2") == 0xFF, idx(fixed, b"SCANTEST2"))
        check("ch3 -> scan list 1", idx(fixed, b"SCANTEST3") == 1, idx(fixed, b"SCANTEST3"))
    if raw.exists():
        print("info: without the translation dmrconf writes", hex(idx(raw, b"SCANTEST1") or 0), "for ch1 (the old bug)")
    check("no temp qdmr-*.yaml left in /tmp", not [p for p in Path("/tmp").glob(f"qdmr-{os.getpid()}-*.yaml")])
    # read back: a decode gives scanListRef, load() maps it to scanList
    back = tmpd / "back.yaml"
    subprocess.run(["dmrconf", "decode", "--radio", "d878uv2", str(fixed), str(back)], capture_output=True, timeout=120)
    if back.exists():
        ws = Path(os.environ["DATA_DIR"]) / "workspace" if os.environ.get("DATA_DIR") else None
        bd = cp._yaml.load(back.read_text())
        cp.from_qdmr(bd)
        by = {str(next(iter(c.values())).get("name")): next(iter(c.values())) for c in bd["channels"]}
        check("decode -> from_qdmr: SCANTEST1 has scanList", bool(by.get("SCANTEST1", {}).get("scanList")), dict(by.get("SCANTEST1", {})))
        check("decode -> from_qdmr: SCANTEST2 has none", not by.get("SCANTEST2", {}).get("scanList"))
else:
    print("SKIP encode checks (no dmrconf)")
shutil.rmtree(tmpd, ignore_errors=True)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
