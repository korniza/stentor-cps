# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""SharkRF openSPOT (2/3/4) over its HTTP API - EXPERIMENTAL, written from the published web-UI API without a
device at hand. Login is the token/digest handshake of the web UI; the rest are the JSON endpoints behind it.
Anything unexpected is reported as an error so the UI stays honest. Set HOTSPOT_KIND=openspot, HOTSPOT_HOST and
HOTSPOT_PASSWORD (the web UI password)."""
import hashlib
import time

import httpx

from . import config

_jwt: dict = {"token": None, "t": 0.0}


def _base() -> str:
    host = config.HOTSPOT_HOST or config.WPSD_HOST
    if not host:
        raise RuntimeError("HOTSPOT_HOST is not set")
    return host if host.startswith("http") else f"http://{host}"


async def _login(c: httpx.AsyncClient) -> str:
    if _jwt["token"] and time.time() - _jwt["t"] < 600:
        return _jwt["token"]
    tok = (await c.get("/gettok.cgi")).json().get("token", "")
    digest = hashlib.sha256((tok + (config.HOTSPOT_PASSWORD or "")).encode()).hexdigest()
    r = (await c.post("/login.cgi", json={"token": tok, "digest": digest})).json()
    if not r.get("success") or not r.get("jwt"):
        raise RuntimeError("openSPOT login failed (password?)")
    _jwt.update({"token": r["jwt"], "t": time.time()})
    return r["jwt"]


async def _get(path: str) -> dict:
    async with httpx.AsyncClient(base_url=_base(), timeout=10) as c:
        jwt = await _login(c)
        r = await c.get(path, headers={"Authorization": f"Bearer {jwt}"})
        if r.status_code == 401:
            _jwt["token"] = None
            jwt = await _login(c)
            r = await c.get(path, headers={"Authorization": f"Bearer {jwt}"})
        r.raise_for_status()
        return r.json()


async def status() -> dict:
    st = await _get("/status.cgi")
    freq = await _get("/modemfreq.cgi")
    mode = await _get("/modemmode.cgi")
    conn = await _get("/connector.cgi")
    return {
        "kind": "openspot", "host": config.HOTSPOT_HOST or config.WPSD_HOST, "hostname": st.get("name") or "openSPOT",
        "uptime": st.get("uptime"), "cpu_temp_c": None,
        "services": {"connector": "active" if st.get("status") in (1, 2, "connected") else str(st.get("status"))},
        "callsign": st.get("callsign") or config.CALLSIGN, "dmr_id": st.get("dmr_id") or config.DMR_ID,
        "duplex": "0", "rx_mhz": (freq.get("rx_freq") or 0) / 1e6 or None, "tx_mhz": (freq.get("tx_freq") or 0) / 1e6 or None,
        "color_code": int(mode.get("dmr_cc", 1) or 1), "location": None, "modem": "openSPOT",
        "modes_enabled": {"DMR": str(mode.get("mode", "")).lower() == "dmr"},
        "dmr_network": {"Type": "openSPOT connector", "Address": conn.get("active_connector")},
        "release": {"Version": st.get("sw_version") or st.get("version")},
        "experimental": True,
    }


async def last_heard(limit: int = 25, include_rf_only: bool = False) -> list[dict]:
    lh = await _get("/lh.cgi")
    out = []
    for e in lh.get("entries", lh.get("lh", []))[:limit]:
        out.append({"time_utc": e.get("time") or e.get("ts"), "mode": e.get("mode", "DMR"), "slot": e.get("ts"),
                    "source": "RF" if e.get("from_modem") else "network", "from": e.get("src") or e.get("callsign"),
                    "to": f"TG {e.get('dst')}" if e.get("dst") else "", "duration_s": e.get("duration"),
                    "ber_pct": e.get("ber"), "loss_pct": None, "rssi_dbm": e.get("rssi")})
    if include_rf_only:
        out = [e for e in out if e["source"] == "RF"]
    return out


async def read_config(which: str, section: str = "") -> dict:
    """Shape the openSPOT connector as a one-network DMRGateway so the sync logic can use it unchanged."""
    if which == "mmdvmhost":
        st = await status()
        return {"file": "openspot", "sections": {
            "General": {"Callsign": st["callsign"], "Duplex": "0"},
            "Info": {"RXFrequency": str(int((st["rx_mhz"] or 0) * 1e6)), "TXFrequency": str(int((st["tx_mhz"] or 0) * 1e6))},
            "DMR": {"Enable": "1", "ColorCode": str(st["color_code"]), "Id": str(st["dmr_id"])}}}
    if which == "dmrgateway":
        conn = await _get("/connector.cgi")
        name = str(conn.get("active_connector_name") or conn.get("active_connector") or "openSPOT")
        server = str(conn.get("server") or conn.get("host") or "")
        return {"file": "openspot", "sections": {"DMR Network 1": {
            "Enabled": "1", "Name": name, "Address": server, "Id": str(conn.get("dmr_id") or config.DMR_ID),
            "PassAllTG1": "1", "PassAllTG2": "2"}}}
    raise ValueError("openSPOT exposes only 'mmdvmhost' and 'dmrgateway' views")


async def service_action(action: str) -> dict:
    if action == "reboot":
        async with httpx.AsyncClient(base_url=_base(), timeout=10) as c:
            jwt = await _login(c)
            r = await c.post("/reboot.cgi", headers={"Authorization": f"Bearer {jwt}"}, json={"reboot": 1})
        return {"ok": r.status_code == 200, "output": "reboot requested"}
    raise ValueError("openSPOT supports only 'reboot' (connectors restart from its web UI)")


async def run(cmd: str, timeout: int = 30) -> tuple[int, str]:
    raise RuntimeError("openSPOT has no shell")
