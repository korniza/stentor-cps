# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Thin async wrapper around the qdmr `dmrconf` CLI (patched build with pty-bridge support)."""
import asyncio
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import config
from . import local as ctx



def _sandbox_preexec(cpu_soft: int):
    """Return a post-fork hook that resource-limits dmrconf (safe limits only; no RLIMIT_AS — Qt reserves
    large virtual memory). Every step is best-effort so a limit it can't set never blocks the exec."""
    def apply():  # runs in the forked child, before execve
        import resource
        for what, lim in ((resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2),   # no multi-GB disk bomb
                          (resource.RLIMIT_CORE, (0, 0)),                        # no core dumps
                          (resource.RLIMIT_NOFILE, (1024, 1024)),               # fd ceiling
                          (resource.RLIMIT_CPU, (cpu_soft, cpu_soft + 60))):     # CPU bomb backstop (wall timeout is primary)
            try:
                resource.setrlimit(what, lim)
            except (ValueError, OSError):
                pass
        try:
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)              # PR_SET_NO_NEW_PRIVS
        except Exception:  # noqa: BLE001
            pass
    return apply
OUTPUT_CAP = 256 * 1024          # dmrconf prints one line per block on a dead link: cap what we keep
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_QT_NOISE = ("Detected locale", "Qt depends on a UTF-8", "If this causes problems", "for more information")

def bridge_link() -> str | None:
    """The pty of the browser bridge (see bridge.py), or None."""
    from . import bridge
    return bridge.link()


def _lock():
    """Programming sessions are serialised: one at a time on the radio."""
    from . import bridge
    return bridge.lock()


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
    if dev.startswith("/dev/pts/") or dev == bridge_link():
        return None
    return dev


def _save_trace(model, args, rc, text, why):
    """Persist a verbose dmrconf trace (DMRCONF_TRACE=1; diagnostics).
    Best-effort; never raises. Files in DATA_DIR/traces, newest 40 kept."""
    try:
        tdir = config.DATA_DIR / "traces"
        tdir.mkdir(parents=True, exist_ok=True)
        try:
            call = ctx.callsign() or "single"
        except Exception:  # noqa: BLE001
            call = "single"
        call = re.sub(r"[^A-Za-z0-9]", "", str(call))[:16] or "single"
        op = next((a for a in args if a in ("read", "write", "verify", "detect", "info")), "op")
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = tdir / f"{model}-{op}-{call}-{ts}.log"
        header = (f"# dmrconf verbose trace\n# time: {datetime.now().isoformat(timespec='seconds')}\n"
                  f"# model: {model}\n# op: {op}\n# callsign: {call}\n# exit: {rc}\n"
                  f"# why: {why or ''}\n# args: {' '.join(args)}\n" + "-" * 60 + "\n")
        path.write_text(header + (text or ""), encoding="utf-8")
        files = sorted(tdir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[40:]:
            try:
                old.unlink()
            except OSError:
                pass
        logging.getLogger("stentor.radio").warning(
            "verbose trace captured: model=%s op=%s call=%s exit=%s file=%s", model, op, call, rc, path.name)
    except Exception:  # noqa: BLE001
        pass


def trace_on() -> bool:
    """DMRCONF_TRACE=1 in the env: keep a verbose dmrconf trace of every run in DATA_DIR/traces (diagnostics)."""
    return os.environ.get("DMRCONF_TRACE", "") == "1"


def _last_progress(text: str) -> str | None:
    m = re.findall(r"(\d{1,3})%", text or "")
    return m[-1] if m else None


def _save_bridge_snapshot(model, args, why, text):
    """A run on the browser bridge failed (port lost, radio stopped answering, timeout): keep the last frames between
    dmrconf and the radio, the last memory address asked for and the progress, so a radio that drops out at the same
    point every time (e.g. a new firmware) can be pinned down. Adds a 'bridge_fail' event to the operation record and
    writes DATA_DIR/traces/<model>-<op>-<call>-<time>-bridge.log (newest 40 traces kept). Never raises."""
    try:
        from . import bridge
        p = bridge.port()
        items = list(p.trace)[-400:]
        last = {}
        for t, d, b in reversed(items):
            if d == ">" and len(b) >= 6 and chr(b[0]) in "RW" and chr(b[0]) not in last:
                last[chr(b[0])] = {"addr": f"0x{int.from_bytes(b[1:5], 'big'):08x}", "len": b[5], "t": round(t, 3)}
            if len(last) == 2:
                break
        run = getattr(getattr(p, "session", None), "relay", None)
        batch_from = getattr(run, "rb_from", -1) if run is not None else -1
        info = {"t": time.time(), "kind": "bridge_fail", "why": why, "progress": _last_progress(text),
                "last_read": last.get("R"), "last_write": last.get("W"),
                "batch_from": f"0x{batch_from:08x}" if isinstance(batch_from, int) and batch_from >= 0 else None,
                "last_frame_age": round(time.time() - items[-1][0], 2) if items else None}
        p.events.append(info)
        tdir = config.DATA_DIR / "traces"
        tdir.mkdir(parents=True, exist_ok=True)
        try:
            call = ctx.callsign() or "single"
        except Exception:  # noqa: BLE001
            call = "single"
        call = re.sub(r"[^A-Za-z0-9]", "", str(call))[:16] or "single"
        op = next((a for a in args if a in ("read", "write", "verify", "detect", "info")), "op")
        path = tdir / f"{model}-{op}-{call}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-bridge.log"
        lines = [f"# browser bridge snapshot", f"# model: {model}", f"# op: {op}", f"# callsign: {call}",
                 f"# why: {why}", f"# progress: {info['progress']}%", f"# last read: {info['last_read']}",
                 f"# last write: {info['last_write']}", f"# batch from: {info['batch_from']}",
                 f"# device: {getattr(p, 'device', None)}", "-" * 60]
        t0 = items[0][0] if items else 0
        for t, d, b in items:
            lines.append(f"{t - t0:9.3f} {d} {len(b):4d} {b[:48].hex(' ')}{' …' if len(b) > 48 else ''}")
        lines += ["-" * 60, (text or "")[-4000:]]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        files = sorted(tdir.glob("*.log"), key=lambda q: q.stat().st_mtime, reverse=True)
        for old in files[40:]:
            try:
                old.unlink()
            except OSError:
                pass
        logging.getLogger("stentor.radio").warning("bridge failure snapshot: model=%s op=%s call=%s at %s%% last read %s file=%s",
                                                  model, op, call, info["progress"], info["last_read"], path.name)
    except Exception:  # noqa: BLE001
        pass


async def run_dmrconf(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """Run dmrconf, return (exit code, cleaned stdout+stderr). A local USB radio is fronted by the protocol relay
    (bridge.serial_relay) so it gets the same frame verification and differential writes as the browser bridge."""
    from . import bridge, codeplug as _cp
    _cp.flush_all()
    ensure_dev_links()
    args = list(args)
    qdmr_tmp = _qdmr_input(args)                       # channel scanList -> scanListRef for dmrconf (0.4.95)
    model = args[args.index("--radio") + 1] if "--radio" in args else None
    trace = bool(model and trace_on())
    if trace and "--loglevel" not in args:
        _i = args.index("--radio")
        args[_i:_i] = ["--loglevel", "trace"]
    relay = None
    dev = _relay_candidate(args) if bridge.USB_RELAY else None
    if dev:
        relay = bridge.serial_relay(dev)
        try:
            link = await relay.__aenter__()
            args[args.index("--device") + 1] = link
        except OSError as exc:                         # busy/gone: fall back to dmrconf opening it directly
            relay = None
            bridge.port().events.append({"t": time.time(), "kind": "usb-relay", "error": str(exc)})
    env = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8", QT_QPA_PLATFORM="offscreen")
    pty_in_use = args[args.index("--device") + 1] if "--device" in args else None
    on_bridge = pty_in_use is not None and pty_in_use == bridge_link()
    try:
        kw = {"preexec_fn": _sandbox_preexec(timeout + 60)} if config.SANDBOX else {}
        proc = await asyncio.create_subprocess_exec(
            "dmrconf", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env, **kw)
        out = bytearray()
        errors = 0
        cap = OUTPUT_CAP * 4 if trace else OUTPUT_CAP
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
                if len(out) > cap:                         # keep head + tail, never megabytes of repeated errors
                    out = out[:cap // 2] + b"\n[...]\n" + out[-cap // 2:]
            elif chunk == b"":
                break                                      # EOF: dmrconf exited
            if on_bridge and bridge_link() != pty_in_use:   # closed, or re-opened as a new pty
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
        if trace:
            _save_trace(model, args, 124 if why else proc.returncode, text, why)
        if why and on_bridge:
            _save_bridge_snapshot(model, args, why, text)
        if why:
            return 124, text + "\nERROR: " + why
        return proc.returncode, text
    finally:
        if relay is not None:
            await relay.__aexit__(None, None, None)
        if qdmr_tmp is not None:
            qdmr_tmp.unlink(missing_ok=True)


def _qdmr_input(args: list[str]) -> Path | None:
    """For dmrconf commands that read a YAML codeplug (write/verify/encode) swap the YAML argument for a temporary
    copy in qdmr's own key names (see codeplug.qdmr_yaml_text). Returns the temp path to delete, or None."""
    from . import codeplug as _cp
    if not args or args[0] not in ("write", "verify", "encode") or "--yaml" not in args:
        return None
    for i, a in enumerate(args):
        if i and str(a).endswith(".yaml") and Path(a).is_file() and args[i - 1] not in ("--device", "--radio", "--loglevel"):
            tmp = Path("/tmp") / f"qdmr-{os.getpid()}-{time.time_ns()}.yaml"
            try:
                if not _cp.qdmr_copy(Path(a), tmp):
                    return None
            except OSError:
                return None
            args[i] = str(tmp)
            return tmp
    return None


# AnyTone family each qdmr model expects, compared with what the radio says in its identify answer ("D878UV2" …)
_FAMILY = {"d878uv2": "D878", "d878uv": "D878", "d868uve": "D868", "d578uv": "D578", "dmr6x2": "6X2"}


def radio_family(ident: str | None) -> str | None:
    up = re.sub(r"[^A-Z0-9]", "", str(ident or "").upper())
    m = re.search(r"D(\d{3})", up)
    return "D" + m.group(1) if m else ("6X2" if "6X2" in up else None)


def wrong_radio(ident: str | None) -> dict | None:
    """None when the connected radio fits the configured model (RADIO_MODEL); else what was found.
    AnyTone radios identify themselves to the relay, TyT-family ones to the DFU bridge."""
    from .dfubridge import ident_matches, is_dfu_model
    if is_dfu_model(ctx.radio_model()):
        if ident_matches(ctx.radio_model(), ident) is not False:
            return None
    else:
        want, got = _FAMILY.get(ctx.radio_model()), radio_family(ident)
        if not want or not got or want == got:
            return None
    return {"detected": ident, "expected_model": ctx.radio_model()}


def _device_args() -> list[str]:
    args = ["--radio", ctx.radio_model()]
    dev = bridge_link() or ctx.radio_device()
    if not dev:                                        # auto: a single local USB serial port is the radio
        cands = [c for c in serial_candidates() if c.startswith("/dev/")]
        if len(cands) == 1:
            dev = cands[0]
    if dev:
        if not Path(dev).exists():
            raise RuntimeError(f"Radio device {dev} not present - is the radio connected and switched on?")
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
    async with _lock():
        args = ["detect"]
        if bridge_link():
            args += ["--radio", ctx.radio_model(), "--device", bridge_link()]
        elif ctx.radio_device():
            args += ["--device", ctx.radio_device()]
        code, out = await run_dmrconf(args, timeout=30)
    return {"ok": code == 0, "output": out, "serial_ports_seen": serial_candidates(),
            "configured_device": bridge_link() or ctx.radio_device() or "(auto)", "model": ctx.radio_model(),
            "via_browser_bridge": bool(bridge_link())}


def _usb_present() -> bool:
    host = Path("/hostdev")
    return any(host.glob("ttyACM*")) or any(host.glob("ttyUSB*")) if host.exists() else bool(serial_candidates())


async def wait_ready(was_bridge: bool, timeout: float = 90.0) -> dict:
    """AnyTone radios reboot after every programming session (USB gone for ~10-20 s). Before a second session
    (e.g. the write after the safety-backup read) wait for the radio to disappear and come back - on the browser
    bridge that means the page re-opening the port (bridge.active again), on the server its ttyACM node."""
    import time as _t
    present = (lambda: bridge_link() is not None) if was_bridge else _usb_present
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
    year_dir = ctx.qdmr_backup_dir() / stamp[:4]
    year_dir.mkdir(parents=True, exist_ok=True)
    model = re.sub(r"[^A-Za-z0-9]+", "", ctx.radio_model() or "radio").upper() or "RADIO"
    base = f"{model}_{stamp}" + (f"_{safe}" if safe else "")
    yaml_path = year_dir / f"{base}.yaml"
    result = {"yaml": str(yaml_path)}
    from . import bridge, fmradio, glistfix
    from .dfubridge import is_dfu_model
    glf = glistfix.affected()
    anytone = not is_dfu_model(ctx.radio_model())
    async with _lock():
        # FM broadcast memories ride along (qdmr has no model; AnyTone only); on a D578 also the group-list slot bitmap
        bridge.plan_reads((fmradio.read_addresses() if anytone else []) + (glistfix.read_addresses() if glf else []))
        code, out = await run_dmrconf(["read", "--yaml", *_device_args(), str(yaml_path)], timeout=config.read_timeout(ctx.radio_model()))
        fm_blocks = bridge.take_overlay()["reads"]
        result["read_output"] = out
        if code != 0 or not yaml_path.exists():
            result["ok"] = False
            return result
        if also_binary:
            await wait_ready(bool(bridge_link()))
            bin_path = year_dir / f"{base}.dfu"
            code2, out2 = await run_dmrconf(["read", "--bin", *_device_args(), str(bin_path)], timeout=config.read_timeout(ctx.radio_model()))
            result["binary"] = str(bin_path) if code2 == 0 else None
            result["binary_output"] = out2
    # a different radio than the configured model: keep the dated backup, but don't let it become last-read —
    # and the write that follows a backup won't happen
    wr = wrong_radio((bridge.last_run() or {}).get("radio"))
    if wr:
        result.update({"ok": False, "wrong_radio": wr,
                       "read_output": (result.get("read_output") or "") + f"\nERROR: the connected radio is a {wr['detected']}, "
                                      f"but RADIO_MODEL is {wr['expected_model']} — nothing was stored as last-read."})
        return result
    if glf:
        enabled = glistfix.decode(fm_blocks)
        if enabled is not None:
            glistfix.save_state(enabled, (bridge.last_run() or {}).get("radio"))
            result["glist_slots"] = len(enabled)
    # the workspace keeps only "last-read" (the NAS tree has the dated copy)
    shutil.copy2(yaml_path, ctx.workspace() / "last-read.yaml")
    if glf:
        try:                                               # spare lists written by Stentor's D578 work-around
            glistfix.strip_pads(ctx.workspace() / "last-read.yaml")
        except Exception:  # noqa: BLE001
            pass
    from . import codeplug as _cp
    _cp.forget("last-read")
    result["workspace_copy"] = "last-read"
    result["backup_rel"] = str(yaml_path.relative_to(ctx.backup_dir())) if yaml_path.is_relative_to(ctx.backup_dir()) else None
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


_APRS_REF = re.compile(r"^\s+aprs:\s+(?!!)[^\s#]+\s*$", re.M)


def uses_aprs(yaml_path: Path) -> bool:
    """True when any channel of the codeplug sends position reports (has an APRS/GPS system set).
    Used to pass --auto-enable-gps on write: dmrconf then switches the radio's GPS on. Without the flag
    dmrconf leaves the radio's GPS on/off byte as it was (an --init-codeplug write leaves it off), and the
    flag is not passed for codeplugs without APRS because dmrconf would then switch GPS off."""
    try:
        text = Path(yaml_path).read_text(errors="replace")
    except OSError:
        return False
    chans = text.split("\nchannels:\n", 1)
    if len(chans) < 2:
        return False
    body = re.split(r"\n[A-Za-z]", chans[1], maxsplit=1)[0]
    return bool(_APRS_REF.search(body))


async def write_codeplug(yaml_path: Path, init_codeplug: bool = False, update_clock: bool = True,
                         fm_list: list[dict] | None = None) -> dict:
    args = ["write", "--yaml", *_device_args()]
    if init_codeplug:
        args.append("--init-codeplug")
    if update_clock:
        args.append("--update-device-clock")
    if uses_aprs(yaml_path):
        args.append("--auto-enable-gps")
    args.append(str(yaml_path))
    from . import bridge, fmradio
    from .dfubridge import is_dfu_model
    if is_dfu_model(ctx.radio_model()):                     # the FM broadcast list is an AnyTone memory area
        fm_list = []
    elif fm_list is None:                                   # default: the FM list kept next to that codeplug
        fm_list = fmradio.load_list(Path(yaml_path).stem)
    async with _lock():
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
    async with _lock():
        code, out = await run_dmrconf(["verify", "--yaml", *_device_args(), str(yaml_path)], timeout=config.read_timeout(ctx.radio_model()))
    return {"ok": code == 0, "output": out}


async def write_callsign_db(limit: int, dmr_id: str, database: str = "") -> dict:
    args = ["write-db", *_device_args(), "--limit", str(limit)]
    if dmr_id:
        args += ["--id", str(dmr_id)]       # the radio's DMR ID can be stored as a number in the settings
    if database:
        args += ["--database", str(database)]
    async with _lock():
        code, out = await run_dmrconf(args, timeout=config.WRITE_TIMEOUT)
    return {"ok": code == 0, "output": out}


async def encode_check(yaml_path: Path) -> dict:
    """Offline validation: encode YAML into a binary for the configured radio."""
    tmp = Path("/tmp") / f"validate-{_stamp()}-{os.getpid()}-{id(yaml_path):x}-{time.time_ns() % 10**6}.dfu"
    code, out = await run_dmrconf(["encode", "--yaml", "--radio", ctx.radio_model(), str(yaml_path), str(tmp)], timeout=60)
    ok = code == 0 and tmp.exists()
    size = tmp.stat().st_size if tmp.exists() else 0
    tmp.unlink(missing_ok=True)
    return {"ok": ok, "binary_size": size, "messages": out}


async def decode_manufacturer(rdt_path: Path, out_yaml: Path) -> dict:
    code, out = await run_dmrconf(
        ["decode", "--manufacturer", "--radio", ctx.radio_model(), str(rdt_path), str(out_yaml)], timeout=120)
    return {"ok": code == 0 and out_yaml.exists(), "messages": out}
