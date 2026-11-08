# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""External data sources: RadioID.net, BrandMeister API v2, RepeaterBook."""
import asyncio
import json
import math
import time
from pathlib import Path

import httpx

from . import config

BM = "https://api.brandmeister.network/v2"
RADIOID = "https://radioid.net/api"
_cache: dict[str, tuple[float, object]] = {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, headers={"User-Agent": config.HTTP_USER_AGENT, "Accept": "application/json"},
                             follow_redirects=True)


async def _get_json(url: str, params: dict | None = None, headers: dict | None = None, ttl: int = 0):
    key = url + repr(sorted((params or {}).items()))
    if ttl and key in _cache and time.time() - _cache[key][0] < ttl:
        return _cache[key][1]
    async with _client() as c:
        r = await c.get(url, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
    if ttl:
        _cache[key] = (time.time(), data)
    return data


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ------------------------------------------------------------------ RadioID
async def radioid_user(callsign: str = "", dmr_id: str = "") -> list[dict]:
    params = {"callsign": callsign} if callsign else {"id": dmr_id}
    data = await _get_json(f"{RADIOID}/dmr/user/", params, ttl=3600)
    return data.get("results", [])


async def radioid_repeaters(country: str = "Greece", city: str = "", callsign: str = "") -> list[dict]:
    params = {"country": country}
    if city:
        params["city"] = city
    if callsign:
        params = {"callsign": callsign}
    data = await _get_json(f"{RADIOID}/dmr/repeater/", params, ttl=6 * 3600)
    seen: dict = {}
    for r in data.get("results", []):
        try:
            rx = float(r.get("frequency"))
            off = float(str(r.get("offset", "0")).replace("+", "") or 0)
        except (TypeError, ValueError):
            continue
        item = {
            "source": "radioid", "callsign": r.get("callsign"), "id": r.get("id"), "city": r.get("city"),
            "state": r.get("state"), "rx_mhz": rx, "tx_mhz": round(rx + off, 5), "offset_mhz": off,
            "color_code": r.get("color_code"), "network": _dmr_net(r.get("ipsc_network")),
            "trustee": r.get("trustee"), "locator": r.get("locator"),
            "talkgroups": r.get("talkgroups") or [], "mode": "DMR",
        }
        # RadioID carries exact duplicate registrations; collapse by callsign+RX, keep the richer one
        key = (str(r.get("callsign")).upper(), round(rx, 4))
        if key not in seen or len(item["talkgroups"]) > len(seen[key]["talkgroups"]):
            seen[key] = item
    return list(seen.values())


_NET_MAP = {"bm": "BM", "brandmeister": "BM", "brandmesiter": "BM", "dmr-plus": "DMR+", "dmr+": "DMR+",
            "dmr plus": "DMR+", "dmrplus": "DMR+", "tgif": "TGIF", "ipsc2 rpt greece": "IPSC2"}


def _dmr_net(v) -> str | None:
    if not v or str(v).lower() in ("none", "null"):
        return None
    s = str(v).strip()
    return _NET_MAP.get(s.lower(), s)


# ------------------------------------------------------------------ RepeaterBook (rest of world)
# Terms of the RepeaterBook API: credit + link wherever their data is shown; no mirroring/bulk export.
RB_ATTRIBUTION = {"text": "Data courtesy of RepeaterBook.com.", "url": "https://www.repeaterbook.com"}


def attribution_for(repeaters: list[dict]) -> dict | None:
    return RB_ATTRIBUTION if any(r.get("source") == "repeaterbook" for r in repeaters) else None


_rb_locks: dict[str, "asyncio.Lock"] = {}
_rb_mem: dict[str, tuple[float, list]] = {}
_rb_fail: dict[str, float] = {}                 # last failed fetch per country -> back off
RB_RETRY_S = 1800


def _rb_cache_file(country: str) -> Path:
    safe = "".join(ch if ch.isalnum() else "_" for ch in country.strip().lower()) or "x"
    return config.DATA_DIR / "cache" / "repeaterbook" / f"{safe}.json"


def _rb_cache_read(country: str) -> tuple[float, list] | None:
    if country in _rb_mem:
        return _rb_mem[country]
    try:
        d = json.loads(_rb_cache_file(country).read_text())
        _rb_mem[country] = (float(d["fetched"]), d["results"])
        return _rb_mem[country]
    except Exception:  # noqa: BLE001 - no/garbled cache = fetch
        return None


def _rb_cache_write(country: str, results: list) -> None:
    _rb_mem[country] = (time.time(), results)
    try:
        f = _rb_cache_file(country)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"country": country, "fetched": _rb_mem[country][0], "results": results}))
        tmp.replace(f)
    except Exception:  # noqa: BLE001 - the in-memory copy still serves
        pass


def repeaterbook_status() -> dict:
    """Admin view: token set, UA in use and which countries are cached (age in hours)."""
    cached = {}
    d = config.DATA_DIR / "cache" / "repeaterbook"
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            j = json.loads(f.read_text())
            cached[j.get("country") or f.stem] = {"count": len(j.get("results") or []),
                                                  "age_h": round((time.time() - float(j["fetched"])) / 3600, 1)}
        except Exception:  # noqa: BLE001
            continue
    return {"token_set": bool(config.REPEATERBOOK_TOKEN), "user_agent": (config.REPEATERBOOK_USER_AGENT or config.HTTP_USER_AGENT), "cached": cached}


async def _rb_fetch(country: str) -> list:
    headers = {"X-RB-App-Token": config.REPEATERBOOK_TOKEN,     # their canonical header (raw token, no Bearer)
               "User-Agent": (config.REPEATERBOOK_USER_AGENT or config.HTTP_USER_AGENT),    # must match the approved UA exactly
               "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get("https://www.repeaterbook.com/api/exportROW.php", params={"country": country}, headers=headers)
    if r.status_code == 429:
        raise RuntimeError("429 rate limited by RepeaterBook")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or "results" not in data:
        raise RuntimeError(f"unexpected answer: {str(data)[:200]}")
    return data.get("results") or []


async def repeaterbook(country: str = "Greece") -> list[dict]:
    """Repeaters of one country (outside North America) from RepeaterBook. One request per country per
    REPEATERBOOK_TTL (default 24 h), cached on disk and shared by all users; on an error a stale copy is served.
    Without a token no request is made (their API answers 401 anyway)."""
    if not config.REPEATERBOOK_TOKEN:
        raise RuntimeError("401 (no token)")
    key = country.strip().lower()
    hit = _rb_cache_read(key)
    if not (hit and time.time() - hit[0] < config.REPEATERBOOK_TTL):
        lock = _rb_locks.setdefault(key, asyncio.Lock())
        async with lock:                                    # concurrent users -> one request
            hit = _rb_cache_read(key)
            fresh = hit and time.time() - hit[0] < config.REPEATERBOOK_TTL
            backoff = time.time() - _rb_fail.get(key, 0) < RB_RETRY_S
            if not fresh and not (hit and backoff):
                try:
                    _rb_cache_write(key, await _rb_fetch(country))
                    _rb_fail.pop(key, None)
                except Exception:
                    _rb_fail[key] = time.time()
                    if not hit:
                        raise
                hit = _rb_cache_read(key)                   # stale copy on error (retried after RB_RETRY_S)
    rows = (_rb_row(r) for r in hit[1])
    return [r for r in rows if r and str(r.get("status") or "").lower() != "off-air"]


def _rb_row(r: dict) -> dict | None:
    try:
        rx = float(r.get("Frequency"))
        tx = float(r.get("Input Freq") or rx)
    except (TypeError, ValueError):
        return None
    modes = [m for m, k in (("FM", "FM Analog"), ("DMR", "DMR"), ("D-STAR", "D-Star"), ("YSF", "System Fusion"),
                            ("NXDN", "NXDN"), ("M17", "M17")) if str(r.get(k, "")).lower() == "yes"]
    bw = str(r.get("FM Bandwidth") or "")
    return {
        "source": "repeaterbook", "rb_id": r.get("Rptr ID"), "callsign": r.get("Callsign"), "city": r.get("Nearest City"),
        "landmark": r.get("Landmark") or None,
        "region": r.get("Region") or r.get("State"), "rx_mhz": rx, "tx_mhz": tx, "offset_mhz": round(tx - rx, 5),
        "ctcss": _tone(r.get("PL")), "tsq": _tone(r.get("TSQ")), "color_code": r.get("DMR Color Code") or None,
        "dmr_id": r.get("DMR ID") or None, "modes": modes, "status": r.get("Operational Status"),
        "use": r.get("Use") or None, "bandwidth": "Narrow" if bw.startswith("12") else ("Wide" if bw else None),
        "echolink": _node(r.get("EchoLink Node")), "allstar": _node(r.get("AllStar Node")),
        "lat": _f(r.get("Lat")), "lon": _f(r.get("Long")), "notes": r.get("Notes") or None,
        "updated": r.get("Last Update") or None,
    }


def _tone(v):
    s = str(v or "").strip()
    try:
        return s if s and float(s) > 0 else None
    except ValueError:
        return None                                         # "CSQ", "D023" etc. - not a CTCSS tone


def _node(v):
    s = str(v or "").strip()
    return s if s and s != "0" else None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ BrandMeister
def _bm_headers(write: bool = False) -> dict:
    if config.BM_API_KEY:
        return {"Authorization": f"Bearer {config.BM_API_KEY}"}
    if write:
        raise RuntimeError("BM_API_KEY is not configured on the server (BrandMeister SelfCare > Profile > API Keys).")
    return {}


async def bm_talkgroups() -> dict:
    return await _get_json(f"{BM}/talkgroup", ttl=24 * 3600)


async def bm_device(device_id: str) -> dict:
    return await _get_json(f"{BM}/device/{device_id}", headers=_bm_headers())


async def bm_device_profile(device_id: str) -> dict:
    return await _get_json(f"{BM}/device/{device_id}/profile", headers=_bm_headers())


async def bm_device_public(device_id: str) -> dict | None:
    """Public BrandMeister device record (no API key needed): callsign, last_seen, rx/tx, coords. None if unknown."""
    try:
        return await _get_json(f"{BM}/device/{device_id}", ttl=6 * 3600)
    except httpx.HTTPStatusError:
        return None


async def bm_liveness(ids: list) -> dict:
    """{bm_id: {last_seen, rx_mhz, tx_mhz, pep}} for repeater IDs (RadioID 'locator'). Concurrent, lightly throttled."""
    import asyncio
    sem = asyncio.Semaphore(8)
    out: dict = {}

    async def one(rid):
        async with sem:
            d = await bm_device_public(str(rid))
        if not d:
            return
        try:
            rx = float(d.get("rx") or 0) or None
            tx = float(d.get("tx") or 0) or None
        except (TypeError, ValueError):
            rx = tx = None
        out[str(rid)] = {"last_seen": d.get("last_seen"), "rx_mhz": rx, "tx_mhz": tx, "pep": d.get("pep")}

    await asyncio.gather(*(one(i) for i in ids if str(i).isdigit() and len(str(i)) >= 6))
    return out


async def bm_add_static(device_id: str, talkgroup: int, slot: int) -> dict:
    async with _client() as c:
        r = await c.post(f"{BM}/device/{device_id}/talkgroup", json={"slot": slot, "group": talkgroup},
                         headers=_bm_headers(True))
        return {"status": r.status_code, "body": r.text[:500]}


async def bm_remove_static(device_id: str, talkgroup: int, slot: int) -> dict:
    async with _client() as c:
        r = await c.delete(f"{BM}/device/{device_id}/talkgroup/{slot}/{talkgroup}", headers=_bm_headers(True))
        return {"status": r.status_code, "body": r.text[:500]}


async def bm_drop_dynamic(device_id: str, slot: int) -> dict:
    async with _client() as c:
        r = await c.get(f"{BM}/device/{device_id}/action/dropDynamicGroups/{slot}", headers=_bm_headers(True))
        return {"status": r.status_code, "body": r.text[:500]}


USERS_URL = "https://radioid.net/static/users.json"


async def radioid_userdb(countries: list[str] | None, max_age_h: int = 24) -> tuple[str, int, dict]:
    """Download (cached) the RadioID user list, filter by country, write a qdmr-compatible JSON file.
    Returns (path, count, per-country counts)."""
    import json as _json
    import os
    import time as _t
    from pathlib import Path
    cache = config.DATA_DIR / "users-cache.json"
    if not cache.exists() or _t.time() - cache.stat().st_mtime > max_age_h * 3600:
        async with httpx.AsyncClient(timeout=180, headers={"User-Agent": config.HTTP_USER_AGENT}, follow_redirects=True) as c:
            r = await c.get(USERS_URL)
            r.raise_for_status()
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            os.replace(tmp, cache)
    users = _json.loads(cache.read_text())["users"]
    if countries:
        want = {c.strip().lower() for c in countries}
        users = [u for u in users if str(u.get("country", "")).strip().lower() in want]
    per = {}
    for u in users:
        per[u.get("country") or "?"] = per.get(u.get("country") or "?", 0) + 1
    tag = "all" if not countries else "-".join(sorted(c.strip().lower().replace(" ", "_") for c in countries))
    out = config.DATA_DIR / f"userdb-{tag}.json"
    out.write_text(_json.dumps({"users": users}, ensure_ascii=False))
    return str(out), len(users), dict(sorted(per.items(), key=lambda kv: -kv[1])[:15])
