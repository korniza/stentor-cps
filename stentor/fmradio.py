# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""FM broadcast stations for the radio's FM receiver (AnyTone D868/D878/D578/6X2: 100 memories, 87.5-108 MHz).

Three parts:
  * a station source per city: radiomap.eu / worldradiomap.com city pages (frequency, station, transmitter site),
    fetched once and cached for a week;
  * a per-codeplug list kept by Stentor next to the YAML (``<codeplug>.fm.json``), since qdmr has no model for the
    FM memories (the radio stores only frequencies; names live here for the UI);
  * the radio image: the WFM channel list (100 x 4-byte BCD, 0x02480000), its bitmap (0x02480210) and the VFO
    (0x02480200), encoded/decoded as 16-byte blocks that the relay writes at the end of a programming session or
    in a short session of its own (see bridge.direct_session)."""
import html
import json
import re
import time
from pathlib import Path

import httpx

from . import config

WFM_LIST = 0x02480000           # 100 entries x 4 bytes BCD big-endian, unit 100 Hz
WFM_VFO = 0x02480200            # 4 bytes BCD, same format
WFM_BITMAP = 0x02480210         # bit n -> entry n valid
MAX_STATIONS = 100
BAND = (64.0, 108.0)
CACHE_TTL = 7 * 86400
UA = "Stentor/0.4 (+https://github.com/korniza/stentor; ham radio codeplug tool)"
SOURCES = ("https://radiomap.eu", "https://worldradiomap.com")
_cache_dir = config.DATA_DIR / "cache" / "fm"

# the city page titles start with "Radio stations in", "Ραδιοφωνικοί σταθμοί στην", "Radiosender in", ...
_TITLE_PREP = re.compile(r"^.*?\s(?:in|στην|στη|στο|στον|στα|στους|στις|en|à|a|da|di|w|v|i|em|din|na|u|på|op|de)\s+", re.I)


# ---------------------------------------------------------------- source: radiomap.eu
def _cache_path(key: str) -> Path:
    _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir / (re.sub(r"[^a-z0-9_-]+", "_", key.lower()) + ".json")


async def _fetch(path: str) -> str:
    last = None
    for base in SOURCES:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as c:
                r = await c.get(f"{base}/{path}")
            if r.status_code == 200 and len(r.text) > 2000:
                return r.text
            last = f"{base}: HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last = f"{base}: {e}"
    raise RuntimeError(f"station list not reachable ({last})")


def _cached(key: str):
    p = _cache_path(key)
    if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL:
        return json.loads(p.read_text())
    return None


def _store(key: str, value) -> None:
    _cache_path(key).write_text(json.dumps(value, ensure_ascii=False))


async def cities(country: str = "gr") -> list[dict]:
    """Cities with a station list for an ISO country code (radiomap.eu/<cc>)."""
    cc = country.lower().strip()
    if not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", cc):
        raise ValueError("country must be an ISO 3166 code like 'gr'")
    hit = _cached(f"cities-{cc}")
    if hit is not None:
        return hit
    page = await _fetch(cc)
    names: dict[str, str] = {}
    for m in re.finditer(rf'href="(?:\.\./{cc}/|/{cc}/|)([a-z0-9-]+)\.htm"(?:\s+title="([^"]*)")?', page):
        slug, title = m.group(1), html.unescape(m.group(2) or "")
        if slug in ("index", "play", "list", "map"):
            continue
        name = ""
        if title:
            first = title.split(",")[0].strip()
            stripped = _TITLE_PREP.sub("", first)
            name = stripped if stripped != first else ""
        if slug not in names or (name and not names[slug]):
            names[slug] = name
    out = [{"slug": slug, "name": name or slug.replace("-", " ").title()} for slug, name in names.items()]
    if not out:
        raise RuntimeError(f"no cities found for '{cc}'")
    _store(f"cities-{cc}", out)
    return out


async def stations(country: str, city: str) -> list[dict]:
    """FM stations of a city page: [{mhz, name, site}] sorted by frequency."""
    cc, slug = country.lower().strip(), city.lower().strip()
    if not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", cc) or not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValueError("bad country/city")
    hit = _cached(f"stations-{cc}-{slug}")
    if hit is not None:
        return hit
    page = await _fetch(f"{cc}/{slug}")
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        cells = re.findall(r"<td[^>]*class=\"?(\w+)\"?[^>]*>(.*?)</td>", row, flags=re.S)
        d, logo = {}, ""
        for cls, body in cells:
            if cls == "fsta":
                m = re.search(r'<img[^>]+src=["\']?([^"\' >]+)["\']?[^>]*class=["\']?station', body, flags=re.I) or \
                    re.search(r'<img[^>]+class=["\']?station["\']?[^>]*src=["\']?([^"\' >]+)', body, flags=re.I)
                if m:
                    logo = m.group(1).replace("../", "").lstrip("/")        # e.g. gr/images/menta88.gif
            text = html.unescape(re.sub(r"<[^>]+>", " ", body))
            text = re.sub(r"\s+", " ", text).strip()
            d.setdefault(cls, text)
        freq = d.get("freq", "").replace(",", ".")
        m = re.search(r"\b(\d{2,3}\.\d{1,2})\b", freq)
        if not m or "fsta" not in d:
            continue
        mhz = float(m.group(1))
        if not BAND[0] <= mhz <= BAND[1]:
            continue
        item = {"mhz": mhz, "name": d.get("fsta", "")[:32], "site": d.get("fpre", "")[:48]}
        if logo and re.fullmatch(r"[a-z0-9_./-]+\.(gif|png|jpg|jpeg|webp)", logo, flags=re.I):
            item["logo"] = logo
        out.append(item)
    out.sort(key=lambda s: s["mhz"])
    if not out:
        raise RuntimeError(f"no stations parsed for {cc}/{slug}")
    _store(f"stations-{cc}-{slug}", out)
    return out


# ---------------------------------------------------------------- per-codeplug list
def _sidecar(codeplug: str) -> Path:
    from . import codeplug as cp
    return config.WORKSPACE / f"{cp.safe_name(codeplug)}.fm.json"


def load_list(codeplug: str) -> list[dict]:
    p = _sidecar(codeplug)
    if not p.exists():
        return []
    try:
        return [_norm(s) for s in json.loads(p.read_text()).get("stations", [])][:MAX_STATIONS]
    except (ValueError, OSError):
        return []


def save_list(codeplug: str, items: list[dict]) -> list[dict]:
    items = dedupe([_norm(s) for s in items])[:MAX_STATIONS]
    doc = {"stations": items, "updated": time.time()}
    old = load_list(codeplug)
    if [s["mhz"] for s in old] == [s["mhz"] for s in items]:      # only names changed: still what the radio holds
        if (w := written_at(codeplug)):
            doc["written_at"] = w
    _sidecar(codeplug).write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    return items


def _norm(s: dict) -> dict:
    mhz = round(float(s.get("mhz") or s.get("frequency") or 0), 2)
    if not BAND[0] <= mhz <= BAND[1]:
        raise ValueError(f"{mhz} MHz is outside the FM band")
    out = {"mhz": mhz, "name": str(s.get("name") or "")[:32]}
    if s.get("area"):
        out["area"] = str(s["area"])[:32]
    if s.get("logo") and re.fullmatch(r"[a-z0-9_./-]+", str(s["logo"]), flags=re.I):
        out["logo"] = str(s["logo"])[:80]
    return out


def mark_written(codeplug: str) -> None:
    p = _sidecar(codeplug)
    try:
        d = json.loads(p.read_text()) if p.exists() else {"stations": []}
    except ValueError:
        d = {"stations": []}
    d["written_at"] = time.time()
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def written_at(codeplug: str) -> float | None:
    p = _sidecar(codeplug)
    try:
        return json.loads(p.read_text()).get("written_at") if p.exists() else None
    except ValueError:
        return None


def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for s in items:
        key = int(round(s["mhz"] * 100))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ---------------------------------------------------------------- radio image <-> list
# The radio stores each frequency as 8 BCD digits in units of 100 Hz, most significant byte first:
# 87.5 MHz -> 875000 -> "00875000" -> 00 87 50 00. (qdmr's element code assumes little-endian here; the D878UVII
# display proved it wrong: bytes 00 50 87 00 show up as "050.87M".)
def _bcd8(value: int) -> bytes:
    return bytes.fromhex(f"{value:08d}"[-8:])


def _from_bcd8(raw: bytes) -> int:
    s = raw[:4].hex()
    return int(s) if s.isdigit() else 0


def encode_blocks(items: list[dict], keep_vfo: bool = True) -> dict[int, bytes | None]:
    """16-byte blocks for the WFM list + bitmap holding `items` (empty entries cleared). With keep_vfo the VFO block
    is marked "keep" (None): the relay rewrites what it read, since the radio erases the sector around it."""
    lst = bytearray(0x200)
    bm = bytearray(0x20)
    for n, s in enumerate(items[:MAX_STATIONS]):
        lst[n * 4:n * 4 + 4] = _bcd8(int(round(s["mhz"] * 1e6)) // 100)
        bm[n // 8] |= 1 << (n % 8)
    blocks = {WFM_LIST + off: bytes(lst[off:off + 16]) for off in range(0, 0x200, 16)}
    blocks.update({WFM_BITMAP + off: bytes(bm[off:off + 16]) for off in range(0, 0x20, 16)})
    if keep_vfo:
        blocks[WFM_VFO] = None
    return blocks


def read_addresses() -> list[int]:
    return [WFM_LIST + off for off in range(0, 0x200, 16)] + [WFM_BITMAP, WFM_BITMAP + 16, WFM_VFO]


def decode_blocks(blocks: dict[int, bytes]) -> dict:
    """{stations: [{mhz}], vfo_mhz} from blocks read off the radio (missing blocks -> partial result)."""
    lst = b"".join(blocks.get(WFM_LIST + off, b"\0" * 16) for off in range(0, 0x200, 16))
    bm = blocks.get(WFM_BITMAP, b"\0" * 16) + blocks.get(WFM_BITMAP + 16, b"\0" * 16)
    out = []
    for n in range(MAX_STATIONS):
        if not bm[n // 8] & (1 << (n % 8)):
            continue
        v = _from_bcd8(lst[n * 4:n * 4 + 4])
        if v:
            out.append({"slot": n + 1, "mhz": round(v * 100 / 1e6, 2)})
    vfo = blocks.get(WFM_VFO)
    return {"stations": out, "vfo_mhz": round(_from_bcd8(vfo[:4]) * 100 / 1e6, 2) if vfo else None,
            "complete": all(a in blocks for a in read_addresses()[:-1])}


_logo_dir = config.DATA_DIR / "cache" / "fm" / "logos"


async def logo_bytes(path: str) -> tuple[bytes, str] | None:
    """Station logo as served by radiomap.eu, cached on disk. path like 'gr/images/menta88.gif'."""
    if not re.fullmatch(r"[a-z0-9_-]+/images/[a-z0-9_.-]+\.(gif|png|jpg|jpeg|webp)", path, flags=re.I):
        return None
    f = _logo_dir / path.replace("/", "__")
    ext = path.rsplit(".", 1)[-1].lower()
    mime = {"gif": "image/gif", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}[ext]
    if f.exists():
        return f.read_bytes(), mime
    _logo_dir.mkdir(parents=True, exist_ok=True)
    for base in SOURCES:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": UA}) as c:
                r = await c.get(f"{base}/{path}")
            if r.status_code == 200 and r.content and len(r.content) < 512 * 1024:
                f.write_bytes(r.content)
                return r.content, mime
        except httpx.HTTPError:
            continue
    return None
