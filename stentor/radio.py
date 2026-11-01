# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Thin async wrapper around the qdmr `dmrconf` CLI (patched build with pty-bridge support)."""
import asyncio
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import config

_radio_lock = asyncio.Lock()
OUTPUT_CAP = 256 * 1024          # dmrconf prints one line per block on a dead link: cap what we keep
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_QT_NOISE = ("Detected locale", "Qt depends on a UTF-8", "If this causes problems", "for more information")

# When a browser bridges its Web Serial port to us (see bridge.py) this holds the pty path to use instead of USB.
bridge_device: str | None = None


def _clean(text: str) -> str:
    lines = []
    for ln in _ANSI.sub("", text).splitlines():
        if re.search(r"\[[= ]*\]\s*\d+%", ln):   # progress bar frames
            continue
        if ln.strip() and not ln.startswith(_QT_NOISE):
            lines.append(ln.rstrip())
    return "\n".join(lines)


def ensure_dev_links() -> None:
    """Host /dev is mounted at /hostdev. qdmr finds radios via udev (/run/udev, mounted) + libusb (/dev/bus/usb)
    and opens /dev/ttyACMx by name, so mirror those nodes into the container's /dev as symlinks."""
    host = Path("/hostdev")
    if not host.exists():
        return
    try:
        bus = Path("/dev/bus")
        if not bus.exists() and (host / "bus").exists():
            bus.symlink_to(host / "bus")
        for pat in ("ttyACM*", "ttyUSB*"):
            for node in host.glob(pat):
                link = Path("/dev") / node.name
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(node)
    except OSError:
        pass


def _relay_candidate(args: list[str]) -> str | None:
    """The local serial device named in `args` that should go through the relay (never a bridge pty)."""
    if "--device" not in args:
        return None
    dev = args[args.index("--device") + 1]
    if dev.startswith("/dev/pts/") or dev == bridge_device:
        return None
    return dev


async def run_dmrconf(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """Run dmrconf, return (exit code, cleaned stdout+stderr). A local USB radio is fronted by the protocol relay
    (bridge.serial_relay) so it gets the same frame verification and differential writes as the browser bridge."""
    from . import bridge, codeplug as _cp
    _cp.flush_all()
    ensure_dev_links()
    args = list(args)
    relay = None
    dev = _relay_candidate(args) if bridge.USB_RELAY else None
    if dev:
        relay = bridge.serial_relay(dev)
        try:
            link = await relay.__aenter__()
            args[args.index("--device") + 1] = link
        except OSError as exc:                         # busy/gone: fall back to dmrconf opening it directly
            relay = None
            bridge._events.append({"t": time.time(), "kind": "usb-relay", "error": str(exc)})
    env = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8", QT_QPA_PLATFORM="offscreen")
    pty_in_use = args[args.index("--device") + 1] if "--device" in args else None
    on_bridge = pty_in_use is not None and pty_in_use == bridge_device
    try:
        proc = await asyncio.create_subprocess_exec(
            "dmrconf", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        out = bytearray()
        errors = 0
        deadline = time.monotonic() + timeout
        why = None
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=1.0)
            except asyncio.TimeoutError:
                chunk = None
            if chunk:
                errors += chunk.count(b"No response from device")
                out += chunk
                if len(out) > OUTPUT_CAP:                  # keep head + tail, never megabytes of repeated errors
                    out = out[:OUTPUT_CAP // 2] + b"\n[...]\n" + out[-OUTPUT_CAP // 2:]
            elif chunk == b"":
                break                                      # EOF: dmrconf exited
            if on_bridge and bridge_device != pty_in_use:   # closed, or re-opened as a new pty
                why = "the browser bridge closed (radio port lost) - dmrconf stopped"
            elif errors >= 50:
                why = "the radio stopped answering - dmrconf stopped"
            elif time.monotonic() > deadline:
                why = f"dmrconf timed out after {timeout}s"
            if why:
                proc.kill()
                break
        await proc.wait()
        text = _clean(out.decode(errors="replace"))
        if why:
            return 124, text + "\nERROR: " + why
        return proc.returncode, text
    finally:
        if relay is not None:
            await relay.__aexit__(None, None, None)


def _device_args() -> list[str]:
    args = ["--radio", config.RADIO_MODEL]
    dev = bridge_device or config.RADIO_DEVICE
    if not dev:                                        # auto: a single local USB serial port is the radio
        cands = [c for c in serial_candidates() if c.startswith("/dev/")]
        if len(cands) == 1:
            dev = cands[0]
    if dev:
        if not Path(dev).exists():
            raise RuntimeError(f"Radio device {dev} not present - is the D878UVII connected and switched on?")
        args += ["--device", dev]
    return args


def serial_candidates() -> list[str]:
    roots = [Path("/hostdev"), Path("/dev")]
    found = []
    for r in roots:
        if r.exists():
            found += [str(p) for p in sorted(r.glob("ttyACM*"))] + [str(p) for p in sorted(r.glob("ttyUSB*"))]
    return found


async def detect() -> dict:
    async with _radio_lock:
        args = ["detect"]
        if bridge_device:
            args += ["--radio", config.RADIO_MODEL, "--device", bridge_device]
        elif config.RADIO_DEVICE:
            args += ["--device", config.RADIO_DEVICE]
        code, out = await run_dmrconf(args, timeout=30)
    return {"ok": code == 0, "output": out, "serial_ports_seen": serial_candidates(),
            "configured_device": bridge_device or config.RADIO_DEVICE or "(auto)", "model": config.RADIO_MODEL,
            "via_browser_bridge": bool(bridge_device)}


def _usb_present() -> bool:
    host = Path("/hostdev")
    return any(host.glob("ttyACM*")) or any(host.glob("ttyUSB*")) if host.exists() else bool(serial_candidates())


async def wait_ready(was_bridge: bool, timeout: float = 90.0) -> dict:
    """AnyTone radios reboot after every programming session (USB gone for ~10-20 s). Before a second session
    (e.g. the write after the safety-backup read) wait for the radio to disappear and come back - on the browser
    bridge that means the page re-opening the port (bridge.active again), on the server its ttyACM node."""
    import time as _t
    present = (lambda: bridge_device is not None) if was_bridge else _usb_present
    t0 = _t.monotonic()
    while _t.monotonic() - t0 < 8 and present():          # phase 1: let it go away (or it never does)
        await asyncio.sleep(0.5)
    went_away = not present()
    while _t.monotonic() - t0 < timeout and not present():  # phase 2: wait for it to return
        await asyncio.sleep(1.0)
    if not present():
        return {"ok": False, "waited": round(_t.monotonic() - t0, 1), "went_away": went_away}
    await asyncio.sleep(2.5 if was_bridge else 3.0)        # settle: enumeration / port open
    if not was_bridge:
        ensure_dev_links()
    return {"ok": True, "waited": round(_t.monotonic() - t0, 1), "went_away": went_away}


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


async def read_codeplug(label: str = "", also_binary: bool = False) -> dict:
    """Read the radio into YAML (and optionally binary) under the NAS backup tree."""
    stamp = _stamp()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-")
    year_dir = config.QDMR_BACKUP_DIR / stamp[:4]
    year_dir.mkdir(parents=True, exist_ok=True)
    base = f"D878UVII_{stamp}" + (f"_{safe}" if safe else "")
    yaml_path = year_dir / f"{base}.yaml"
    result = {"yaml": str(yaml_path)}
    from . import bridge, fmradio
    async with _radio_lock:
        bridge.plan_reads(fmradio.read_addresses())        # FM broadcast memories ride along (qdmr has no model)
        code, out = await run_dmrconf(["read", "--yaml", *_device_args(), str(yaml_path)], timeout=config.READ_TIMEOUT)
        fm_blocks = bridge.take_overlay()["reads"]
        result["read_output"] = out
        if code != 0 or not yaml_path.exists():
            result["ok"] = False
            return result
        if also_binary:
            await wait_ready(bool(bridge_device))
            bin_path = year_dir / f"{base}.dfu"
            code2, out2 = await run_dmrconf(["read", "--bin", *_device_args(), str(bin_path)], timeout=config.READ_TIMEOUT)
            result["binary"] = str(bin_path) if code2 == 0 else None
            result["binary_output"] = out2
    # the workspace keeps only "last-read" (the NAS tree has the dated copy)
    shutil.copy2(yaml_path, config.WORKSPACE / "last-read.yaml")
    from . import codeplug as _cp
    _cp.forget("last-read")
    result["workspace_copy"] = "last-read"
    result["backup_rel"] = str(yaml_path.relative_to(config.BACKUP_DIR)) if yaml_path.is_relative_to(config.BACKUP_DIR) else None
    result["relay"] = bridge.last_run()
    if fm_blocks:
        fm = fmradio.decode_blocks(fm_blocks)
        if fm["complete"]:
            names = {s["mhz"]: s["name"] for s in fmradio.load_list("current")}
            lst = [{"mhz": x["mhz"], "name": names.get(x["mhz"], "")} for x in fm["stations"]]
            fmradio.save_list("last-read", lst)
            shutil.copy2(fmradio._sidecar("last-read"), yaml_path.with_suffix(".fm.json"))   # next to the NAS backup
            if not fmradio.load_list("current"):
                fmradio.save_list("current", lst)
            result["fm_stations"] = len(lst)
    result["ok"] = True
    return result


async def write_codeplug(yaml_path: Path, init_codeplug: bool = False, update_clock: bool = True) -> dict:
    args = ["write", "--yaml", *_device_args()]
    if init_codeplug:
        args.append("--init-codeplug")
    if update_clock:
        args.append("--update-device-clock")
    args.append(str(yaml_path))
    from . import bridge, fmradio
    fm_list = fmradio.load_list(Path(yaml_path).stem)
    async with _radio_lock:
        if fm_list:
            # written by the relay just before END; dmrconf itself rewrites the VFO block in update mode, and in
            # init mode the relay keeps whatever it read for it
            bridge.plan_reads([fmradio.WFM_VFO])
            bridge.plan_writes(fmradio.encode_blocks(fm_list))
        code, out = await run_dmrconf(args, timeout=config.WRITE_TIMEOUT)
        ov = bridge.take_overlay()
    return {"ok": code == 0, "output": out, "relay": bridge.last_run(),
            "fm": {"stations": len(fm_list), **(ov["writes"] or {})} if fm_list else None}


async def verify_codeplug(yaml_path: Path) -> dict:
    async with _radio_lock:
        code, out = await run_dmrconf(["verify", "--yaml", *_device_args(), str(yaml_path)], timeout=config.READ_TIMEOUT)
    return {"ok": code == 0, "output": out}


async def write_callsign_db(limit: int, dmr_id: str, database: str = "") -> dict:
    args = ["write-db", *_device_args(), "--limit", str(limit)]
    if dmr_id:
        args += ["--id", dmr_id]
    if database:
        args += ["--database", database]
    async with _radio_lock:
        code, out = await run_dmrconf(args, timeout=config.WRITE_TIMEOUT)
    return {"ok": code == 0, "output": out}


async def encode_check(yaml_path: Path) -> dict:
    """Offline validation: encode YAML into a binary for the configured radio."""
    tmp = Path("/tmp") / f"validate-{_stamp()}.dfu"
    code, out = await run_dmrconf(["encode", "--yaml", "--radio", config.RADIO_MODEL, str(yaml_path), str(tmp)], timeout=60)
    ok = code == 0 and tmp.exists()
    size = tmp.stat().st_size if tmp.exists() else 0
    tmp.unlink(missing_ok=True)
    return {"ok": ok, "binary_size": size, "messages": out}


async def decode_manufacturer(rdt_path: Path, out_yaml: Path) -> dict:
    code, out = await run_dmrconf(
        ["decode", "--manufacturer", "--radio", config.RADIO_MODEL, str(rdt_path), str(out_yaml)], timeout=120)
    return {"ok": code == 0 and out_yaml.exists(), "messages": out}
