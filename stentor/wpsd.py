# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""WPSD (W0CHP Pi-Star fork) and Pi-Star hotspot access over SSH - same config files, services and log layout;
only the release file differs (/etc/WPSD-release vs /etc/pistar-release)."""
import re
from pathlib import Path

import asyncssh

from . import config

CONFIG_FILES = {
    "mmdvmhost": "/etc/mmdvmhost",
    "dmrgateway": "/etc/dmrgateway",
    "ysfgateway": "/etc/ysfgateway",
    "dstarrepeater": "/etc/dstarrepeater",
    "release": "/etc/WPSD-release",      # falls back to /etc/pistar-release below
}
SERVICES = ("mmdvmhost", "dmrgateway", "ysfgateway", "dgidgateway", "nxdngateway", "p25gateway", "dapnetgateway")
_SECRET = re.compile(r"^(\s*(Password|Pass|APIKey|ApiKey|Key|Token|Secret)\s*=\s*).*$", re.I)

# MMDVMHost log examples:
# M: 2026-10-28 16:50:01.123 DMR Slot 2, received network voice header from SV1ABC to TG 202
# M: 2026-10-28 16:50:04.321 DMR Slot 2, received network end of voice transmission from SV1ABC to TG 202, 3.2 seconds, 0% packet loss, BER: 0.0%
# M: ... DMR Slot 2, received RF end of voice transmission from N0CALL to TG 202, 2.1 seconds, BER: 0.3%, RSSI: -47/-47/-47 dBm
_LH = re.compile(
    r"^\w: (?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ (?P<mode>DMR Slot (?P<slot>\d)|YSF|D-Star|NXDN|P25|M17), "
    r"received (?P<src>RF|network) (?P<what>voice header|end of voice transmission|late entry|data header|end of transmission)"
    r"(?: from (?P<from>\S+))?(?: to (?P<to>(?:TG )?\S+))?(?P<rest>.*)$")


def _mask(text: str) -> str:
    return "\n".join(_SECRET.sub(r"\1********", ln) for ln in text.splitlines())


async def _connect():
    kw = dict(host=config.WPSD_HOST, port=config.WPSD_PORT, username=config.WPSD_USER, known_hosts=None,
              connect_timeout=10)
    key = Path(config.WPSD_KEY_FILE)
    if key.exists():
        kw["client_keys"] = [str(key)]
    if config.WPSD_PASSWORD:
        kw["password"] = config.WPSD_PASSWORD
    if "client_keys" not in kw and "password" not in kw:
        raise RuntimeError("No WPSD credentials: set WPSD_PASSWORD or install the server's public key on the hotspot "
                           "(see wpsd_ssh_pubkey).")
    return await asyncssh.connect(**kw)


async def run(cmd: str, timeout: int = 30) -> tuple[int, str]:
    async with await _connect() as conn:
        r = await conn.run(cmd, check=False, timeout=timeout)
        return r.exit_status, (r.stdout or "") + (r.stderr or "")


def parse_ini(text: str, sections: list[str] | None = None) -> dict:
    out, cur = {}, None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("#", ";")):
            continue
        m = re.fullmatch(r"\[(.+)\]", ln)
        if m:
            cur = m.group(1)
            continue
        if cur and "=" in ln and (not sections or cur in sections):
            k, v = ln.split("=", 1)
            out.setdefault(cur, {})[k.strip()] = v.strip()
    return out


async def status() -> dict:
    svc = " ".join(SERVICES)
    cmd = ("hostname; uptime -p; cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; "
           f"for s in {svc}; do printf '%s=%s\\n' $s $(systemctl is-active $s.service 2>/dev/null); done; "
           "echo ---; cat /etc/mmdvmhost; echo ---; cat /etc/WPSD-release 2>/dev/null || cat /etc/pistar-release 2>/dev/null")
    code, out = await run(cmd)
    head, mmdvm, rel = (out.split("\n---\n") + ["", ""])[:3]
    lines = head.splitlines()
    temp = None
    if len(lines) > 2 and lines[2].strip().isdigit():
        temp = round(int(lines[2]) / 1000, 1)
    services = {}
    for ln in lines[3:]:
        if "=" in ln:
            k, v = ln.split("=", 1)
            if v and v != "inactive" or k in ("mmdvmhost", "dmrgateway"):
                services[k] = v or "unknown"
    cfg = parse_ini(mmdvm, ["General", "Info", "Modem", "DMR", "DMR Network", "System Fusion", "D-Star", "POCSAG"])
    gen, info, dmr, net = (cfg.get(s, {}) for s in ("General", "Info", "DMR", "DMR Network"))
    modes = {m: cfg.get(m, {}).get("Enable") == "1" for m in ("DMR", "System Fusion", "D-Star", "NXDN", "P25", "POCSAG") if m in cfg}
    return {
        "host": config.WPSD_HOST, "hostname": lines[0] if lines else None, "uptime": lines[1] if len(lines) > 1 else None,
        "cpu_temp_c": temp, "services": services,
        "callsign": gen.get("Callsign"), "dmr_id": dmr.get("Id") or gen.get("Id"), "duplex": gen.get("Duplex"),
        "rx_mhz": _hz(info.get("RXFrequency")), "tx_mhz": _hz(info.get("TXFrequency")),
        "color_code": int(dmr.get("ColorCode", "1") or 1),
        "location": info.get("Location"), "modem": cfg.get("Modem", {}).get("Hardware") or cfg.get("Modem", {}).get("Port"),
        "modes_enabled": modes, "dmr_network": {k: net.get(k) for k in ("Enable", "Type", "Address", "Port", "Slot1", "Slot2") if k in net},
        "release": parse_ini("[r]\n" + rel).get("r", {}),
    }


def _hz(v):
    try:
        return int(v) / 1e6
    except (TypeError, ValueError):
        return None


async def last_heard(limit: int = 25, include_rf_only: bool = False) -> list[dict]:
    cmd = ("f=$(ls -1t /var/log/pi-star/MMDVM-*.log 2>/dev/null | head -2 | tac); "
           "[ -n \"$f\" ] && cat $f | grep -E 'received (RF|network) (end of voice|voice header|end of transmission)' | tail -n 600")
    code, out = await run(cmd)
    events = []
    for ln in out.splitlines():
        m = _LH.match(ln)
        if not m or "end of" not in m.group("what"):
            continue
        if include_rf_only and m.group("src") != "RF":
            continue
        rest = m.group("rest")
        dur = re.search(r"([\d.]+) seconds", rest)
        ber = re.search(r"BER: ([\d.]+)%", rest)
        loss = re.search(r"(\d+)% packet loss", rest)
        rssi = re.search(r"RSSI: (\S+) dBm", rest)
        events.append({
            "time_utc": m.group("ts"), "mode": "DMR" if m.group("slot") else m.group("mode"),
            "slot": int(m.group("slot")) if m.group("slot") else None, "source": m.group("src"),
            "from": m.group("from"), "to": (m.group("to") or "").rstrip(","),
            "duration_s": float(dur.group(1)) if dur else None, "ber_pct": float(ber.group(1)) if ber else None,
            "loss_pct": int(loss.group(1)) if loss else None, "rssi_dbm": rssi.group(1) if rssi else None,
        })
    return list(reversed(events))[:limit]


async def read_config(which: str, section: str = "") -> dict:
    path = CONFIG_FILES.get(which)
    if not path:
        raise ValueError(f"Unknown config '{which}'. Valid: {', '.join(CONFIG_FILES)}")
    code, out = await run(f"cat {path} 2>/dev/null || cat /etc/pistar-release" if which == "release" else f"cat {path}")
    if code != 0:
        raise RuntimeError(out.strip() or f"cannot read {path}")
    parsed = parse_ini(_mask(out) if which != "release" else "[release]\n" + out, [section] if section else None)
    return {"file": path, "sections": parsed}


async def service_action(action: str) -> dict:
    cmds = {
        "restart_mmdvmhost": "sudo systemctl restart mmdvmhost.service",
        "restart_dmrgateway": "sudo systemctl restart dmrgateway.service",
        "restart_all": "sudo systemctl restart mmdvmhost.service dmrgateway.service",
        "reboot": "sudo systemctl reboot",
    }
    if action not in cmds:
        raise ValueError(f"action must be one of {list(cmds)}")
    try:
        code, out = await run(cmds[action], timeout=60)
    except (asyncssh.ConnectionLost, OSError) as e:
        if action == "reboot":
            return {"ok": True, "output": "connection closed (rebooting)"}
        raise e
    return {"ok": code == 0, "output": out.strip()}


def ensure_keypair() -> str:
    key = Path(config.WPSD_KEY_FILE)
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        k = asyncssh.generate_private_key("ssh-ed25519", comment=f"stentor@{config.CALLSIGN}")
        k.write_private_key(str(key))
        key.chmod(0o600)
        k.write_public_key(str(key) + ".pub")
    return Path(str(key) + ".pub").read_text().strip()
