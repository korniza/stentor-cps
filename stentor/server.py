# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Stentor core - AnyTone AT-D878UVII Plus (qdmr/dmrconf) + RepeaterBook/RadioID/BrandMeister + WPSD."""
import hashlib
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Literal


from . import codeplug as cp

from . import config, data, hotspot, radio, sync


# ------------------------------------------------------------------ confirmation tokens
_pending: dict[str, tuple[float, str]] = {}


def _issue(action_key: str) -> str:
    tok = secrets.token_hex(4)
    _pending[tok] = (time.time() + 600, action_key)
    return tok


def _check(tok: str, action_key: str) -> bool:
    now = time.time()
    for k in [k for k, (exp, _) in _pending.items() if exp < now]:
        _pending.pop(k, None)
    entry = _pending.get(tok or "")
    if entry and entry[1] == action_key:
        _pending.pop(tok, None)
        return True
    return False


def _file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


# ================================================================== RADIO
async def radio_detect() -> dict:
    """Check whether the D878UVII is connected to the server (USB) and recognised by dmrconf."""
    return await radio.detect()


async def radio_read(label: str = "", also_binary: bool = False) -> dict:
    """Read the full codeplug from the radio. Saves YAML (and optional raw binary) to the NAS backup tree
    (qdmr/<year>/ under the backup tree, with the FM memories next to it) and into the workspace as 'last-read'.
    Takes 1-3 minutes. label: short note appended to the file name."""
    res = await radio.read_codeplug(label, also_binary)
    if res.get("ok"):
        doc = cp.load(res["workspace_copy"])
        res["overview"] = cp.overview(doc)
    return res


async def radio_write(codeplug: str = "current", init_codeplug: bool = False, confirm_token: str = "",
                      skip_backup: bool = False) -> dict:
    """Write a workspace codeplug to the radio.
    Step 1 (no confirm_token): validates it, diffs against the last radio read and returns a token.
    Step 2 (with token, after the user agrees): reads a fresh safety backup from the radio, then writes.
    init_codeplug=True wipes settings not represented in the YAML (normally keep False).
    skip_backup=True skips the safety backup read - only for recovery, when the radio's current content cannot be
    decoded any more and a known-good backup is being written back."""
    path = cp.path_for(codeplug)
    if not path.exists():
        raise FileNotFoundError(f"codeplug '{codeplug}' not in workspace")
    key = f"write:{path.name}:{_file_hash(path)}:{init_codeplug}:{skip_backup}"
    if not confirm_token:
        val = await radio.encode_check(path)
        last = config.WORKSPACE / "last-read.yaml"
        diff = cp.diff_text(cp.normalized(last), cp.normalized(path), "last-read", codeplug) if last.exists() else "(no previous radio read to diff against)"
        return {"step": "preview", "validation": val, "diff_vs_last_read": diff[:20000],
                "diff_truncated": len(diff) > 20000, "confirm_token": _issue(key) if val["ok"] else None,
                "note": "Show this to the user. Call again with confirm_token only after explicit approval."}
    if not _check(confirm_token, key):
        return {"ok": False, "error": "invalid/expired token or the codeplug changed since the preview - preview again"}
    was_bridge = radio.bridge_device is not None
    if skip_backup:
        res = await radio.write_codeplug(path, init_codeplug=init_codeplug)
        res["safety_backup"] = None
        if res.get("ok"):
            _mark_written(codeplug)
        return res
    backup = await radio.read_codeplug("pre-write")
    if not backup.get("ok"):
        backup["read_output"] = "\n".join(ln for ln in backup.get("read_output", "").splitlines() if not ln.startswith("Warning"))[-4000:]
        return {"ok": False, "error": "safety backup read failed - nothing was written", "backup": backup}
    # the radio reboots after the backup read; wait for it (and, on the browser bridge, for the page to reconnect)
    ready = await radio.wait_ready(was_bridge)
    if not ready["ok"]:
        return {"ok": False, "error": "radio did not come back after the safety backup (reboot) - nothing was written",
                "wait": ready, "safety_backup": backup["yaml"]}
    res = await radio.write_codeplug(path, init_codeplug=init_codeplug)
    res["safety_backup"] = backup["yaml"]
    res["wait"] = ready
    if res.get("ok"):
        _mark_written(codeplug)
    return res


def _mark_written(codeplug: str) -> None:
    """After a successful write the radio holds `codeplug`: make last-read (YAML + FM memories) reflect that."""
    from . import fmradio
    if codeplug != "last-read":
        shutil.copy2(cp.path_for(codeplug), cp.path_for("last-read"))
        cp.forget("last-read")
        fm = fmradio._sidecar(codeplug)
        if fm.exists():
            shutil.copy2(fm, fmradio._sidecar("last-read"))
    fmradio.mark_written(codeplug)


async def radio_verify(codeplug: str = "current") -> dict:
    """Compare a workspace codeplug with what is in the radio (dmrconf verify). Read-only."""
    return await radio.verify_codeplug(cp.path_for(codeplug))


async def radio_write_callsign_db(countries: list[str] | None = None, all_users: bool = False, limit: int = 0,
                                  dmr_id: str = "", confirm_token: str = "") -> dict:
    """Write the Digital Contacts (callsign DB) to the radio from the RadioID.net user list (it shows the caller's
    callsign/name/city on the display). countries=["Greece"] (or ["Greece","Cyprus"]) loads only those countries;
    all_users=True loads the whole world (~315k, fits the D878UVII's 500k but takes much longer to write).
    Replaces the DB currently in the radio. Two-step: call without confirm_token to see counts and get a token."""
    dmr_id = dmr_id or config.DMR_ID
    if not countries and not all_users:
        countries = ["Greece"]
    path, count, per = await data.radioid_userdb(None if all_users else countries)
    n = min(limit, count) if limit else count
    key = f"cdb:{path}:{n}:{dmr_id}"
    if not confirm_token:
        return {"step": "preview", "source": "RadioID.net users.json", "selection": "all users" if all_users else countries,
                "entries": n, "per_country": per,
                "note": "Replaces the radio's current Digital Contacts list. Radio must stay on and connected.",
                "confirm_token": _issue(key)}
    if not _check(confirm_token, key):
        return {"ok": False, "error": "invalid/expired token"}
    return await radio.write_callsign_db(n, dmr_id, path)


# ================================================================== CODEPLUG FILES
def codeplug_list(include_backups: bool = True) -> dict:
    """List workspace codeplugs, radio-read backups on the NAS and CPS .rdt backups."""
    res = {"workspace": cp.list_workspace()}
    if include_backups:
        res["qdmr_backups"] = sorted(str(p.relative_to(config.BACKUP_DIR)) for p in config.QDMR_BACKUP_DIR.rglob("*.yaml"))[-30:] if config.QDMR_BACKUP_DIR.exists() else []
        res["backups"] = [{"path": str(p.relative_to(config.BACKUP_DIR)), "name": p.stem, "size": p.stat().st_size,
                           "mtime": p.stat().st_mtime, "fm": p.with_suffix(".fm.json").exists()}
                          for p in sorted(config.QDMR_BACKUP_DIR.rglob("*.yaml"), key=lambda q: q.stat().st_mtime, reverse=True)[:40]] if config.QDMR_BACKUP_DIR.exists() else []
        res["cps_rdt_backups"] = sorted(str(p.relative_to(config.BACKUP_DIR)) for p in config.CPS_CODEPLUG_DIR.rglob("*.rdt"))[-30:] if config.CPS_CODEPLUG_DIR.exists() else []
    return res


def codeplug_copy(source: str, target: str, overwrite: bool = False) -> dict:
    """Copy a workspace codeplug (e.g. 'last-read' -> 'current'), or import a NAS backup by its relative path
    (as listed under qdmr_backups)."""
    dst = cp.path_for(target)
    if dst.exists() and not overwrite:
        raise FileExistsError(f"'{target}' exists - pass overwrite=True")
    src = config.BACKUP_DIR / source if source.startswith(("qdmr/", "codeplugs/")) else cp.path_for(source)
    if not src.resolve().is_relative_to(config.BACKUP_DIR.resolve()) and not src.resolve().is_relative_to(config.WORKSPACE.resolve()):
        raise ValueError("source outside allowed folders")
    shutil.copy2(src, dst)
    cp.forget(target)
    from . import fmradio
    fm_src = (src.with_suffix(".fm.json") if source.startswith(("qdmr/", "codeplugs/")) else fmradio._sidecar(source))
    if fm_src.exists():                                           # the FM memories travel with the codeplug
        shutil.copy2(fm_src, fmradio._sidecar(target))
    return {"ok": True, "target": target}


async def codeplug_import_rdt(rdt_path: str, target: str = "") -> dict:
    """Convert an AnyTone CPS .rdt file (from the NAS 'codeplugs/' folder, e.g. 'codeplugs/latest.rdt', or an
    absolute path inside the backup tree) into a workspace YAML codeplug using dmrconf's manufacturer-format decoder.
    The web UI uploads .rdt files to 'codeplugs/uploads/'."""
    src = Path(rdt_path) if rdt_path.startswith("/") else config.BACKUP_DIR / rdt_path
    src = src.resolve()
    if not src.is_relative_to(config.BACKUP_DIR.resolve()) or not src.exists():
        raise FileNotFoundError(rdt_path)
    target = cp.safe_name(target or f"cps-{src.stem}")
    res = await radio.decode_manufacturer(src, cp.path_for(target))
    if res["ok"]:
        cp.forget(target)
        res["overview"] = cp.overview(cp.load(target))
        res["target"] = target
    return res


def codeplug_export_cps(codeplug: str = "current") -> dict:
    """Export a workspace codeplug as the AnyTone CPS CSV set (Channel, TalkGroups, Zone, ScanList, RadioIDList,
    ReceiveGroupCallList, RoamingChannel, RoamingZone) zipped under the backup tree 'exports/'. Import them in the
    CPS one table at a time (Tool -> Import). Headers follow CPS 3.0x; see README.txt inside the zip."""
    from . import cps
    doc = cp.load(codeplug)
    files = cps.export_csv(doc)
    out_dir = config.BACKUP_DIR / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{cp.safe_name(codeplug)}-{radio._stamp()}-cps.zip"
    out.write_bytes(cps.export_zip(doc))
    return {"ok": True, "zip": str(out), "rows": {k: max(0, len(v.splitlines()) - 1) for k, v in files.items()}}


def codeplug_new(name: str, callsign: str = "", dmr_id: int = 0) -> dict:
    """Create a minimal empty codeplug in the workspace (radio ID + empty lists)."""
    p = cp.path_for(name)
    if p.exists():
        raise FileExistsError(name)
    doc = cp.new_minimal(callsign or config.CALLSIGN, dmr_id or int(config.DMR_ID or 0))
    cp.save(name, doc)
    return {"ok": True, "name": name}


def codeplug_overview(codeplug: str = "current") -> dict:
    """Counts per section, radio IDs, key settings and zones of a workspace codeplug."""
    return cp.overview(cp.load_ro(codeplug))


def codeplug_get(codeplug: str = "current",
                 section: Literal["channels", "contacts", "groupLists", "zones", "scanLists", "positioning",
                                  "radioIDs", "roamingChannels", "roamingZones"] = "channels",
                 query: str = "", ids: list[str] | None = None, full: bool = False, limit: int = 200) -> dict:
    """List entries of a section. query filters by name substring (case-insensitive) or exact number/frequency
    text; ids selects specific entries; full=True returns every field (otherwise a compact summary)."""
    doc = cp.load_ro(codeplug)
    res = []
    q = query.lower()
    for kind, body in cp.items(doc, section) if doc.get(section) else []:
        b = cp.plain(body)
        if ids and b.get("id") not in ids:
            continue
        if q and q not in str(b.get("name", "")).lower() and q not in str(b.get("number", "")) \
                and q not in str(b.get("rxFrequency", "")):
            continue
        res.append({"kind": kind, **b} if full and kind else (b if full else cp.summary_line(section, kind, body)))
        if len(res) >= limit:
            break
    return {"section": section, "count": len(res), "entries": res}


def codeplug_settings(codeplug: str = "current", path: str = "settings", value: object = None, set_value: bool = False) -> dict:
    """Read (default) or set a value by dotted path, e.g. path='settings.introLine1', set_value=True, value='N0CALL'.
    Works for any top-level block (settings, commercial, sms ...)."""
    doc = cp.load(codeplug)
    if not set_value:
        return {"path": path, "value": cp.get_path(doc, path)}
    old = cp.set_path(doc, path, value)
    cp.save(codeplug, doc)
    return {"ok": True, "path": path, "old": old, "new": value}


def codeplug_upsert(codeplug: str, section: Literal["channels", "contacts", "groupLists", "zones", "scanLists",
                                                    "positioning", "radioIDs", "roamingChannels", "roamingZones"],
                    entry: dict, kind: str = "") -> dict:
    """Generic add/update in qdmr YAML terms. If entry.id exists the given fields are merged (null removes a field);
    otherwise a new entry is created with the next free id. kind is needed for new channels (fm|dmr|am),
    contacts (dmr|dtmf), positioning (dmr|aprs). Frequencies may be numbers (MHz). References use ids."""
    doc = cp.load(codeplug)
    ident = cp.upsert(doc, section, entry, kind or None)
    cp.save(codeplug, doc)
    return {"ok": True, "id": ident}


def codeplug_delete(codeplug: str, section: str, ids: list[str]) -> dict:
    """Delete entries by id; references in zones, group lists, scan lists and channels are cleared too."""
    doc = cp.load(codeplug)
    res = cp.delete(doc, section, ids)
    cp.save(codeplug, doc)
    return res


def codeplug_add_talkgroup(codeplug: str, number: int, name: str, group_lists: list[str] | None = None,
                           call_type: Literal["GroupCall", "PrivateCall", "AllCall"] = "GroupCall") -> dict:
    """Add (or reuse) a DMR contact/talkgroup and optionally append it to RX group lists (ids or names;
    missing group lists are created)."""
    doc = cp.load(codeplug)
    existing = next((b.get("id") for _, b in cp.items(doc, "contacts") if doc.get("contacts")
                     and str(b.get("number")) == str(number) and b.get("type") == call_type), None)
    cid = existing or cp.upsert(doc, "contacts", {"name": name[:16], "ring": False, "type": call_type, "number": int(number)}, "dmr")
    added = []
    for gl in group_lists or []:
        gid = cp.resolve(doc, "groupLists", gl) or cp.upsert(doc, "groupLists", {"name": gl[:16], "contacts": []})
        _, body = cp.find(doc, "groupLists", gid)
        if cid not in body["contacts"]:
            body["contacts"].append(cid)
            added.append(gid)
    cp.save(codeplug, doc)
    return {"ok": True, "contact_id": cid, "reused": bool(existing), "added_to_group_lists": added}


def codeplug_add_channel(codeplug: str, name: str, mode: Literal["dmr", "fm"], rx_mhz: float, tx_mhz: float | None = None,
                         offset_mhz: float | None = None, color_code: int = 1, time_slot: Literal[1, 2] = 1,
                         contact: str = "", group_list: str = "", ctcss_tx: float | None = None,
                         ctcss_rx: float | None = None, bandwidth: Literal["Narrow", "Wide"] = "Wide",
                         power: Literal["Min", "Low", "Mid", "High", "Max", ""] = "", rx_only: bool = False,
                         zones: list[str] | None = None, radio_id: str = "") -> dict:
    """Convenience: add a DMR or FM channel and put it into zones (ids or names; created if missing).
    tx_mhz or offset_mhz (e.g. -7.6 for UHF, -0.6 for VHF). contact/group_list accept id, name or TG number."""
    doc = cp.load(codeplug)
    if tx_mhz is None:
        tx_mhz = round(rx_mhz + (offset_mhz or 0.0), 5)
    entry = {"name": name[:16], "rxFrequency": rx_mhz, "txFrequency": tx_mhz, "rxOnly": rx_only, "admit": "Always"}
    if power:
        entry["power"] = power
    if mode == "dmr":
        cont = cp.resolve(doc, "contacts", contact) if contact else None
        if contact and not cont and str(contact).isdigit():
            cont = next((b.get("id") for _, b in cp.items(doc, "contacts") if doc.get("contacts") and str(b.get("number")) == str(contact)), None)
            if not cont:
                cont = cp.upsert(doc, "contacts", {"name": f"TG {contact}", "ring": False, "type": "GroupCall", "number": int(contact)}, "dmr")
        entry.update({"colorCode": int(color_code), "timeSlot": f"TS{time_slot}",
                      "groupList": cp.resolve(doc, "groupLists", group_list) if group_list else None,
                      "contact": cont, "radioId": cp.resolve(doc, "radioIDs", radio_id) if radio_id else None})
        entry["admit"] = "ColorCode"
    else:
        entry.update({"bandwidth": bandwidth,
                      "txTone": {"ctcss": f"{ctcss_tx:g} Hz"} if ctcss_tx else None,
                      "rxTone": {"ctcss": f"{ctcss_rx:g} Hz"} if ctcss_rx else None, "admit": "Free"})
    entry = {k: v for k, v in entry.items() if v is not None}
    chid = cp.upsert(doc, "channels", entry, mode)
    zone_ids = []
    for z in zones or []:
        zid = cp.resolve(doc, "zones", z) or cp.upsert(doc, "zones", {"name": z[:16], "A": [], "B": []})
        _, body = cp.find(doc, "zones", zid)
        if chid not in body["A"]:
            body["A"].append(chid)
        zone_ids.append(zid)
    cp.save(codeplug, doc)
    return {"ok": True, "channel_id": chid, "zones": zone_ids}


def presets_list() -> dict:
    """Ready-made FM channel sets ("special categories"): PMR446, LPD433, FRS/GMRS, GMRS repeaters, Marine VHF,
    NOAA Weather, ISS, SSTV. Returns each preset's id, label, region and channel count."""
    from . import presets
    return {"presets": presets.catalog()}


def codeplug_add_preset(codeplug: str, preset: str, zone: str = "") -> dict:
    """Add a ready-made FM channel set (see presets_list) as channels in a zone (created if missing). Channels whose
    RX+TX already exist in the codeplug are skipped. zone defaults to the preset's name."""
    from . import presets
    chans = presets.build(preset)
    zone = (zone or presets.default_zone(preset))[:16]
    doc = cp.load(codeplug)
    # map existing channels by (rx, tx) so a preset reuses them instead of duplicating
    by_freq: dict = {}
    for _, b in cp.items(doc, "channels"):
        try:
            key = (round(float(str(b.get("rxFrequency")).split()[0]), 5), round(float(str(b.get("txFrequency")).split()[0]), 5))
        except (TypeError, ValueError):
            continue
        by_freq.setdefault(key, b.get("id"))
    created, reused, zone_ids = 0, 0, []
    for c in chans:
        key = (round(c["rx"], 5), round(c["tx"], 5))
        cid = by_freq.get(key)
        if cid:
            reused += 1
        else:
            entry = {"name": c["name"][:16], "rxFrequency": c["rx"], "txFrequency": c["tx"], "rxOnly": c["rx_only"],
                     "admit": "Free", "bandwidth": "Wide" if c["bw"] == "w" else "Narrow"}
            if c.get("tone"):
                entry["txTone"] = {"ctcss": f"{c['tone']:g} Hz"}
            cid = cp.upsert(doc, "channels", {k: v for k, v in entry.items() if v is not None}, "fm")
            by_freq[key] = cid
            created += 1
        zone_ids.append(cid)
    # the zone always holds the full preset (new + reused channels)
    zid = cp.resolve(doc, "zones", zone) or cp.upsert(doc, "zones", {"name": zone, "A": [], "B": []})
    _, zbody = cp.find(doc, "zones", zid)
    for cid in zone_ids:
        if cid not in zbody["A"]:
            zbody["A"].append(cid)
    cp.save(codeplug, doc)
    return {"ok": True, "preset": preset, "zone": zone, "channels": len(zone_ids), "created": created, "reused": reused}


def codeplug_zone_set(codeplug: str, zone: str, channels: list[str], side: Literal["A", "B"] = "A",
                      mode: Literal["replace", "append", "remove"] = "replace") -> dict:
    """Set/append/remove channels (ids or names) of a zone (created if missing)."""
    doc = cp.load(codeplug)
    zid = cp.resolve(doc, "zones", zone) or cp.upsert(doc, "zones", {"name": zone[:16], "A": [], "B": []})
    _, body = cp.find(doc, "zones", zid)
    ids, missing = [], []
    for c in channels:
        cid = cp.resolve(doc, "channels", c)
        (ids if cid else missing).append(cid or c)
    lst = body.get(side) or cp.to_ruamel([])
    if mode == "replace":
        lst = cp.to_ruamel(ids)
    elif mode == "append":
        for i in ids:
            if i not in lst:
                lst.append(i)
    else:
        for i in ids:
            while i in lst:
                lst.remove(i)
    body[side] = lst
    cp.save(codeplug, doc)
    return {"ok": True, "zone": zid, side: list(lst), "unknown_channels": missing}


async def codeplug_import_repeaters(codeplug: str, repeaters: list[dict], zone: str,
                                    dmr_slots: list[Literal[1, 2]] = [1, 2], default_talkgroup: int = 9,
                                    group_list: str = "") -> dict:
    """Add repeaters (objects as returned by repeaters_search) as channels into a zone. DMR repeaters get one
    channel per time slot in dmr_slots (TS1/TS2, default contact = default_talkgroup), FM repeaters get an
    FM channel with their CTCSS tone. Names are '<callsign> TSn' / '<callsign> FM' (16 chars max)."""
    created = []
    for r in repeaters:
        call = str(r.get("callsign") or "RPT")[:10]
        rx, tx = r.get("rx_mhz"), r.get("tx_mhz")
        modes = r.get("modes") or [r.get("mode", "DMR")]
        if "DMR" in modes:
            cc = int(r.get("color_code") or 1)
            for ts in dmr_slots:
                created.append(codeplug_add_channel(codeplug, f"{call} TS{ts}", "dmr", rx, tx, color_code=cc,
                                                    time_slot=ts, contact=str(default_talkgroup),
                                                    group_list=group_list, zones=[zone])["channel_id"])
        if "FM" in modes:
            tone = r.get("ctcss")
            try:
                tone = float(tone) if tone else None
            except ValueError:
                tone = None
            created.append(codeplug_add_channel(codeplug, f"{call} FM", "fm", rx, tx, ctcss_tx=tone,
                                                bandwidth="Wide", zones=[zone])["channel_id"])
    return {"ok": True, "channels_created": created, "zone": zone}


async def codeplug_validate(codeplug: str = "current") -> dict:
    """Offline check: encode the YAML into a D878UVII binary with dmrconf (no radio needed). Reports errors/warnings."""
    return await radio.encode_check(cp.path_for(codeplug))


def codeplug_diff(a: str = "last-read", b: str = "current", context: int = 2) -> dict:
    """Unified diff between two workspace codeplugs."""
    ta, tb = cp.normalized(cp.path_for(a)), cp.normalized(cp.path_for(b))
    d = cp.diff_text(ta, tb, a, b, context)
    return {"identical": not d, "diff": d[:30000], "truncated": len(d) > 30000}


async def radio_read_blocks(address: int, count: int = 16) -> dict:
    """Diagnostics: read `count` 16-byte blocks starting at `address` straight from the radio (short programming
    session; the radio reboots afterwards). Returns hex per block. count <= 256."""
    from . import bridge
    count = max(1, min(int(count), 256))
    base = int(address) & ~0xF
    async with radio._radio_lock:
        res = await bridge.direct_session(reads=[base + 16 * i for i in range(count)])
    blocks = res["reads"]
    return {"radio": res.get("radio"), "seconds": res.get("seconds"), "missing": count - len(blocks),
            "blocks": {f"{a:08x}": blocks[a].hex(" ") for a in sorted(blocks)}}


# ================================================================== FM BROADCAST
async def fm_cities(country: str = "gr") -> dict:
    """Cities with an FM broadcast station list (source: radiomap.eu / worldradiomap.com), by ISO country code."""
    from . import fmradio
    return {"country": country.lower(), "cities": await fmradio.cities(country)}


async def fm_stations(country: str, city: str) -> dict:
    """FM broadcast stations of a city (frequency MHz, name, transmitter site). city = slug from fm_cities."""
    from . import fmradio
    st = await fmradio.stations(country, city)
    return {"country": country.lower(), "city": city, "count": len(st), "stations": st, "source": "radiomap.eu"}


def fm_list(codeplug: str = "current") -> dict:
    """The FM broadcast memories Stentor keeps for a codeplug (written to the radio's FM receiver with radio_write
    or fm_radio_write). Up to 100 entries; the radio stores only frequencies, names are kept here."""
    from . import fmradio
    lst = fmradio.load_list(codeplug)
    return {"codeplug": codeplug, "count": len(lst), "max": fmradio.MAX_STATIONS, "stations": lst,
            "written_at": fmradio.written_at(codeplug)}


def fm_list_set(codeplug: str, stations: list[dict], mode: Literal["replace", "append"] = "replace") -> dict:
    """Set the FM broadcast memories of a codeplug: stations = [{mhz, name}] (e.g. picked from fm_stations).
    mode=append merges into the existing list; duplicates by frequency are dropped; 100 max (extra are cut)."""
    from . import fmradio
    items = (fmradio.load_list(codeplug) if mode == "append" else []) + list(stations)
    saved = fmradio.save_list(codeplug, items)
    return {"ok": True, "codeplug": codeplug, "count": len(saved), "dropped": max(0, len(items) - len(saved)),
            "note": "Written to the radio by radio_write (together with the codeplug) or fm_radio_write (FM only)."}


async def fm_radio_read() -> dict:
    """Read the FM broadcast memories currently in the radio (short session, the radio reboots afterwards)."""
    from . import bridge, fmradio
    async with radio._radio_lock:
        res = await bridge.direct_session(reads=fmradio.read_addresses())
    fm = fmradio.decode_blocks(res["reads"])
    names = {s["mhz"]: s["name"] for s in fmradio.load_list("current")}
    out = [{"mhz": x["mhz"], "name": names.get(x["mhz"], ""), "slot": x["slot"]} for x in fm["stations"]]
    if fm["complete"]:
        fmradio.save_list("last-read", out)
    return {"ok": fm["complete"], "radio": res.get("radio"), "count": len(out), "stations": out,
            "vfo_mhz": fm["vfo_mhz"], "seconds": res.get("seconds")}


async def fm_radio_write(codeplug: str = "current", confirm_token: str = "") -> dict:
    """Write only the FM broadcast memories of a codeplug to the radio (seconds; the radio reboots afterwards).
    Two-step: call without confirm_token for a preview + token, then again with it after the user agrees."""
    from . import bridge, fmradio
    lst = fmradio.load_list(codeplug)
    key = f"fm:{codeplug}:{hashlib.sha1(repr(lst).encode()).hexdigest()}"
    if not confirm_token:
        return {"step": "preview", "count": len(lst), "stations": lst, "confirm_token": _issue(key),
                "note": "Replaces all FM memories in the radio. Call again with confirm_token after approval."}
    if not _check(confirm_token, key):
        return {"ok": False, "error": "invalid/expired token or the list changed since the preview - preview again"}
    async with radio._radio_lock:
        res = await bridge.direct_session(reads=[fmradio.WFM_VFO], writes=fmradio.encode_blocks(lst))
    w = res["writes"] or {}
    ok = not w.get("failed")
    if ok:
        fmradio.mark_written(codeplug)
        shutil.copy2(fmradio._sidecar(codeplug), fmradio._sidecar("last-read")) if fmradio._sidecar(codeplug).exists() else None
    return {"ok": ok, "count": len(lst), "radio": res.get("radio"), "seconds": res.get("seconds"), **w}


# ================================================================== DATA SOURCES
async def _find_repeaters(country: str, mode: str, near_lat, near_lon, radius_km: float, band: str, city: str,
                          source: str, region: str = "") -> tuple[list[dict], list[str]]:
    results, errors = [], []
    if source in ("auto", "repeaterbook"):
        try:
            results += await data.repeaterbook(country)
        except Exception as e:  # noqa: BLE001
            errors.append(f"repeaterbook: {e}")
    if source == "radioid" or (source == "auto" and (mode in ("any", "DMR"))):
        try:
            rid = await data.radioid_repeaters(country, city)
            have = {(r.get("callsign"), round(r["rx_mhz"], 4)) for r in results}
            results += [r for r in rid if (r["callsign"], round(r["rx_mhz"], 4)) not in have]
        except Exception as e:  # noqa: BLE001
            errors.append(f"radioid: {e}")
    out = []
    for r in results:
        modes = r.get("modes") or [r.get("mode")]
        if mode != "any" and mode not in modes:
            continue
        if band == "2m" and not 144 <= r["rx_mhz"] <= 148:
            continue
        if band == "70cm" and not 430 <= r["rx_mhz"] <= 440:
            continue
        if city and city.lower() not in str(r.get("city", "")).lower():
            continue
        if region and region.lower() not in f"{r.get('region', '')} {r.get('state', '')} {r.get('city', '')}".lower():
            continue
        if near_lat is not None and near_lon is not None:
            if r.get("lat") is None:
                continue
            r["distance_km"] = round(data.haversine_km(near_lat, near_lon, r["lat"], r["lon"]), 1)
            if r["distance_km"] > radius_km:
                continue
        out.append(r)
    out.sort(key=lambda r: (r.get("distance_km", 0), r.get("callsign") or ""))
    return out, errors


async def repeaters_search(country: str = "Greece", mode: Literal["any", "DMR", "FM"] = "any",
                           near_lat: float | None = None, near_lon: float | None = None, radius_km: float = 50,
                           band: Literal["any", "2m", "70cm"] = "any", city: str = "", region: str = "",
                           source: Literal["auto", "radioid", "repeaterbook"] = "auto", limit: int = 40) -> dict:
    """Find repeaters. DMR data from RadioID.net (always free), FM+DMR with coordinates/CTCSS from RepeaterBook
    (may need REPEATERBOOK_TOKEN). near_lat/near_lon + radius_km sort and filter by distance (RepeaterBook only
    has coordinates; RadioID gives locator). region matches RepeaterBook's region/state or the city text."""
    out, errors = await _find_repeaters(country, mode, near_lat, near_lon, radius_km, band, city, source, region)
    return {"count": len(out), "repeaters": out[:limit], "errors": errors, "attribution": data.attribution_for(out)}


async def repeaters_liveness(locators: list) -> dict:
    """Cross-check DMR repeaters against BrandMeister's public device records by their BM ID (the 'locator' from
    repeaters_search). Returns {bm_id: {last_seen, rx_mhz, tx_mhz, pep}}. A repeater not seen for a long time (or
    absent) on BrandMeister may still be live on DMR+/TGIF; last_seen only reflects BrandMeister."""
    return {"liveness": await data.bm_liveness(locators)}


async def codeplug_import_region(codeplug: str, zone: str, country: str = "Greece", region: str = "", city: str = "",
                                 near_lat: float | None = None, near_lon: float | None = None, radius_km: float = 50,
                                 band: Literal["any", "2m", "70cm"] = "any", talkgroups: list[dict] | None = None,
                                 include_fm: bool = True, roaming: bool = True, group_list: str = "",
                                 power: Literal["Min", "Low", "Mid", "High", "Max", ""] = "", max_repeaters: int = 40,
                                 dry_run: bool = False, repeaters: list[dict] | None = None) -> dict:
    """One-step regional import: find the repeaters of a country/region/city (or within radius_km of a point) and
    create, per DMR repeater, one channel per entry of talkgroups=[{tg, ts}] (default [{tg: 9, ts: 2}]), optional
    FM channels with their CTCSS, a zone (split into '<zone> 2', ... above 250 channels) and - for the D878UV family -
    a roaming zone with one roaming channel per DMR repeater. Repeaters already in the codeplug (same RX/TX/slot/TG)
    are reused - they still go into the zone and roaming zone, so both are complete. dry_run=True only reports what
    would be done. repeaters=[...] (objects from repeaters_search) skips the search and imports exactly those."""
    tgs = talkgroups or [{"tg": 9, "ts": 2}]
    for tg in tgs:
        if int(tg.get("ts", 2)) not in (1, 2) or int(tg.get("tg", 0)) <= 0:
            raise ValueError(f"bad talkgroup spec {tg}: need tg>0 and ts in (1, 2)")
    if repeaters:                                            # an explicit selection (e.g. from the web UI)
        found, errors = list(repeaters), []
    else:
        found, errors = await _find_repeaters(country, "any", near_lat, near_lon, radius_km, band, city, "auto", region)
    found = found[:max_repeaters]
    doc = cp.load(codeplug)

    def fkey(rx, tx):
        return (round(float(str(rx).split()[0]), 4), round(float(str(tx).split()[0]), 4))

    # index existing channels (by rx/tx/slot/TG) and roaming channels (by rx/tx) so selected repeaters already in
    # the codeplug are reused - they still go into the zone and the roaming zone instead of being dropped
    cont_num = {b.get("id"): b.get("number") for _, b in cp.items(doc, "contacts") if b.get("id")}
    chan_idx: dict = {}
    for _, b in cp.items(doc, "channels"):
        try:
            k = fkey(b.get("rxFrequency"), b.get("txFrequency"))
        except (TypeError, ValueError):
            continue
        ts = str(b.get("timeSlot", "")).replace("TS", "") or None
        chan_idx.setdefault((k[0], k[1], ts, cont_num.get(b.get("contact"))), b.get("id"))
    roam_idx: dict = {}
    for _, b in cp.items(doc, "roamingChannels"):
        try:
            roam_idx.setdefault(fkey(b.get("rxFrequency"), b.get("txFrequency")), b.get("id"))
        except (TypeError, ValueError):
            continue

    plan = []
    for r in found:
        modes = r.get("modes") or [r.get("mode", "DMR")]
        plan.append({"callsign": str(r.get("callsign") or "RPT"), "rx": r["rx_mhz"], "tx": r["tx_mhz"],
                     "dmr": "DMR" in modes, "fm": include_fm and "FM" in modes, "cc": int(r.get("color_code") or 1),
                     "ctcss": r.get("ctcss"), "city": r.get("city"), "distance_km": r.get("distance_km"),
                     "source": r.get("source")})
    summary = {"found": len(found), "errors": errors, "channels_per_dmr_repeater": len(tgs),
               "repeaters": plan, "attribution": data.attribution_for(found)}
    if dry_run or not plan:
        return {"ok": True, "dry_run": True, **summary}

    def zone_for(n_channels: int) -> str:
        idx = n_channels // 250
        name = zone[:16] if idx == 0 else f"{zone[:13]} {idx + 1}"
        return cp.resolve(doc, "zones", name) or cp.upsert(doc, "zones", {"name": name, "A": [], "B": []})

    created, reused, roam_ids, n_in_zone, zones_used = 0, 0, [], 0, []
    out = []
    for rp in plan:
        call = rp["callsign"][:9]
        chans = []
        if rp["dmr"]:
            for tg in tgs:
                num, ts = int(tg["tg"]), int(tg.get("ts", 2))
                chid = chan_idx.get((round(rp["rx"], 4), round(rp["tx"], 4), str(ts), num))
                if chid:
                    reused += 1
                else:
                    cont = next((b.get("id") for _, b in cp.items(doc, "contacts") if str(b.get("number")) == str(num)), None)
                    if not cont:
                        cont = cp.upsert(doc, "contacts", {"name": f"TG {num}", "ring": False, "type": "GroupCall", "number": num}, "dmr")
                    entry = {"name": f"{call} {num}"[:16], "rxFrequency": rp["rx"], "txFrequency": rp["tx"], "admit": "ColorCode",
                             "colorCode": rp["cc"], "timeSlot": f"TS{ts}", "contact": cont}
                    if group_list:
                        entry["groupList"] = cp.resolve(doc, "groupLists", group_list)
                    if power:
                        entry["power"] = power
                    chid = cp.upsert(doc, "channels", {k: v for k, v in entry.items() if v is not None}, "dmr")
                    chan_idx[(round(rp["rx"], 4), round(rp["tx"], 4), str(ts), num)] = chid
                    created += 1
                chans.append(chid)
        if rp["fm"]:
            chid = chan_idx.get((round(rp["rx"], 4), round(rp["tx"], 4), None, None))
            if chid:
                reused += 1
            else:
                try:
                    tone = float(rp["ctcss"]) if rp["ctcss"] else None
                except ValueError:
                    tone = None
                entry = {"name": f"{call} FM"[:16], "rxFrequency": rp["rx"], "txFrequency": rp["tx"], "admit": "Free", "bandwidth": "Wide"}
                if tone:
                    entry["txTone"] = {"ctcss": f"{tone:g} Hz"}
                if power:
                    entry["power"] = power
                chid = cp.upsert(doc, "channels", entry, "fm")
                created += 1
            chans.append(chid)
        for chid in chans:
            zid = zone_for(n_in_zone)
            if zid not in zones_used:
                zones_used.append(zid)
            _, zb = cp.find(doc, "zones", zid)
            if chid not in zb["A"]:
                zb["A"].append(chid)
            n_in_zone += 1
        if rp["dmr"] and roaming:
            rc = roam_idx.get((round(rp["rx"], 4), round(rp["tx"], 4)))
            if not rc:
                rc = cp.upsert(doc, "roamingChannels", {"name": rp["callsign"][:16], "rxFrequency": rp["rx"], "txFrequency": rp["tx"],
                                                        "colorCode": rp["cc"], "timeSlot": f"TS{int(tgs[0].get('ts', 2))}"})
                roam_idx[(round(rp["rx"], 4), round(rp["tx"], 4))] = rc
            roam_ids.append((rc, [c for c in chans if c]))
        out.append({"callsign": rp["callsign"], "channels": chans})
    roaming_zones = []
    if roam_ids:
        for i in range(0, len(roam_ids), 64):
            chunk = roam_ids[i:i + 64]
            name = zone[:16] if i == 0 else f"{zone[:13]} {i // 64 + 1}"
            rzid = cp.resolve(doc, "roamingZones", name)
            if rzid:
                _, rzb = cp.find(doc, "roamingZones", rzid)
                for rc, _c in chunk:
                    if rc not in rzb["channels"]:
                        rzb["channels"].append(rc)
            else:
                rzid = cp.upsert(doc, "roamingZones", {"name": name, "channels": [rc for rc, _ in chunk]})
            roaming_zones.append(rzid)
            for _rc, cids in chunk:
                for chid in cids:
                    _, cb = cp.find(doc, "channels", chid)
                    if cb is not None and "colorCode" in cb:
                        cb["roaming"] = rzid
    cp.save(codeplug, doc)
    return {"ok": True, "dry_run": False, "repeaters_added": len(out), "channels": created + reused,
            "created": created, "reused": reused, "zones": zones_used, "roaming_zones": roaming_zones, **summary}


async def dmr_id_lookup(callsign: str = "", dmr_id: str = "") -> dict:
    """Look up a DMR ID / callsign on RadioID.net."""
    if not callsign and not dmr_id:
        callsign = config.CALLSIGN
    return {"results": await data.radioid_user(callsign, dmr_id)}


async def talkgroups_search(query: str = "", prefix: str = "", limit: int = 60) -> dict:
    """Search BrandMeister talkgroups by name text and/or numeric prefix (e.g. prefix='202' for Greece)."""
    import unicodedata

    def fold(t: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", str(t).lower()) if unicodedata.category(c) != "Mn")

    tgs = await data.bm_talkgroups()
    q = fold(query)
    hits = [{"number": int(k), "name": v} for k, v in tgs.items()
            if (not prefix or k.startswith(prefix)) and (not q or q in fold(v))]
    hits.sort(key=lambda x: x["number"])
    return {"count": len(hits), "talkgroups": hits[:limit]}


async def bm_device_info(device_id: str = "") -> dict:
    """BrandMeister device (hotspot/repeater) status and profile (static/dynamic/timed talkgroups).
    device_id defaults to the WPSD's DMR ID (read from the hotspot)."""
    if not device_id:
        device_id = await _hotspot_bm_id()
    res = {"device_id": device_id}
    try:
        res["device"] = await data.bm_device(device_id)
    except Exception as e:  # noqa: BLE001
        res["device_error"] = str(e)
    try:
        res["profile"] = await data.bm_device_profile(device_id)
    except Exception as e:  # noqa: BLE001
        res["profile_error"] = str(e)
    return res


async def bm_static_talkgroup(action: Literal["add", "remove", "drop_dynamic"], talkgroup: int = 0, slot: Literal[0, 1, 2] = 0,
                              device_id: str = "", confirm_token: str = "") -> dict:
    """Add/remove a static talkgroup on a BrandMeister device (slot 0 = simplex hotspot), or drop dynamic
    groups on a slot. Needs BM_API_KEY. Two-step with confirm_token."""
    device_id = device_id or await _hotspot_bm_id()
    key = f"bm:{action}:{talkgroup}:{slot}:{device_id}"
    if not confirm_token:
        return {"step": "preview", "action": f"{action} TG {talkgroup} slot {slot} on BM device {device_id}",
                "confirm_token": _issue(key)}
    if not _check(confirm_token, key):
        return {"ok": False, "error": "invalid/expired token"}
    if action == "add":
        return await data.bm_add_static(device_id, talkgroup, slot)
    if action == "remove":
        return await data.bm_remove_static(device_id, talkgroup, slot)
    return await data.bm_drop_dynamic(device_id, slot)


async def _hotspot_bm_id() -> str:
    """BrandMeister device id = the Id of the DMRGateway network pointing at a BrandMeister master
    (e.g. 123456701), falling back to the MMDVMHost DMR id."""
    try:
        gw = (await hotspot.read_config("dmrgateway"))["sections"]
        for sec, kv in gw.items():
            if sec.startswith("DMR Network") and kv.get("Enabled") == "1" and "brandmeister" in kv.get("Address", "").lower():
                return kv["Id"]
    except Exception:  # noqa: BLE001
        pass
    st = await hotspot.status()
    if not st.get("dmr_id"):
        raise RuntimeError("Could not read the DMR ID from the hotspot; pass device_id")
    return str(st["dmr_id"])


# ================================================================== WPSD
async def wpsd_status() -> dict:
    """Hotspot (WPSD / Pi-Star over ssh, openSPOT over HTTP - see HOTSPOT_KIND): services, uptime, CPU temperature,
    frequencies, colour code, enabled modes, DMR network target."""
    return await hotspot.status()


async def wpsd_last_heard(limit: int = 25, rf_only: bool = False) -> dict:
    """Recent transmissions seen by the hotspot (from MMDVMHost logs): callsign, TG, slot, duration, BER, loss."""
    return {"events": await hotspot.last_heard(limit, rf_only)}


async def wpsd_config(which: Literal["mmdvmhost", "dmrgateway", "ysfgateway", "dstarrepeater", "release"] = "mmdvmhost",
                      section: str = "") -> dict:
    """Read a hotspot config file (INI parsed, passwords masked). section narrows to one INI section."""
    return await hotspot.read_config(which, section)


async def wpsd_service(action: Literal["restart_mmdvmhost", "restart_dmrgateway", "restart_all", "reboot"],
                       confirm_token: str = "") -> dict:
    """Restart hotspot services or reboot it (openSPOT: reboot only). Two-step with confirm_token."""
    key = f"wpsd:{action}"
    if not confirm_token:
        return {"step": "preview", "action": f"{action} on {hotspot.kind()} {config.HOTSPOT_HOST}", "confirm_token": _issue(key)}
    if not _check(confirm_token, key):
        return {"ok": False, "error": "invalid/expired token"}
    return await hotspot.service_action(action)


def wpsd_ssh_pubkey() -> dict:
    """Return this server's SSH public key - append it to ~/.ssh/authorized_keys of the hotspot user once
    (WPSD / Pi-Star backends)."""
    return {"user": config.WPSD_USER, "host": config.WPSD_HOST, "public_key": hotspot.ssh_pubkey(),
            "how": f"ssh {config.WPSD_USER}@{config.WPSD_HOST} \"mkdir -p ~/.ssh && echo '<key>' >> ~/.ssh/authorized_keys\""}


def server_info() -> dict:
    """Server configuration (no secrets) and dmrconf/paths status."""
    from . import bridge
    return {
        "version": config.VERSION, "radio_model": config.RADIO_MODEL, "radio_device": config.RADIO_DEVICE or "(auto)",
        "browser_bridge": bridge.status()["active"], "diff_write": bridge.DIFF_WRITE, "usb_relay": bridge.USB_RELAY,
        "hotspot": {"kind": hotspot.kind(), "host": config.HOTSPOT_HOST or None},
        "callsign": config.CALLSIGN, "dmr_id": config.DMR_ID or None,
        "workspace": str(config.WORKSPACE), "backup_dir": str(config.BACKUP_DIR),
        "backup_dir_mounted": config.BACKUP_DIR.exists() and any(config.BACKUP_DIR.iterdir()),
        "brandmeister_api_key": bool(config.BM_API_KEY), "repeaterbook_token": bool(config.REPEATERBOOK_TOKEN),
        "wpsd": f"{config.WPSD_USER}@{config.WPSD_HOST}", "wpsd_key_present": Path(config.WPSD_KEY_FILE).exists(),
        "time": datetime.now().isoformat(timespec="seconds"),
    }


# ================================================================== SYNC + DASHBOARD
def _default_codeplug() -> str | None:
    for n in ("current", "last-read"):
        if cp.path_for(n).exists():
            return n
    ws = sorted(config.WORKSPACE.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)
    return ws[0].stem if ws else None


async def codeplug_sync_wpsd(codeplug: str = "current", zone: str = sync.DEFAULT_ZONE,
                             extra_talkgroups: list[dict] | None = None, dmrplus_talkgroups: list[dict] | None = None,
                             include_defaults: bool = True, remove_extra: bool = True, dry_run: bool = False,
                             create_if_missing: bool = True) -> dict:
    """Sync WPSD -> codeplug. Reads the hotspot live (frequencies, colour code, duplex slots, DMRGateway networks and
    TG rewrites) plus BrandMeister static/dynamic/timed subscriptions, and reconciles a managed zone ('WPSD Hotspot')
    in the codeplug: one DMR channel 'HS <TG> TS<n>' per talkgroup with the radio-side frequencies (radio RX = hotspot
    TX), contacts, RX group lists 'WPSD TS1/TS2' and the DMR ID. Idempotent; only channels named 'HS ...' in that zone
    are managed. extra_talkgroups / dmrplus_talkgroups: [{"tg": 2021, "slot": 2, "name": "..."}]; DMR+ TGs are
    translated through the gateway rewrites (e.g. TG 9 on DMR+ TS1 -> radio TG 8000009). dry_run=True only reports.
    Edits the workspace file only - use radio_write to send it to the radio."""
    info = await sync.gather()
    want = sync.desired(info, extra_talkgroups, dmrplus_talkgroups, include_defaults)
    path = cp.path_for(codeplug)
    if not path.exists():
        if not create_if_missing:
            raise FileNotFoundError(codeplug)
        cp.save(codeplug, cp.new_minimal(config.CALLSIGN, int(info["status"].get("dmr_id") or config.DMR_ID or 0)))
    doc = cp.load(codeplug)
    before = sync.diff(doc, want, zone)
    res = {"codeplug": codeplug, "hotspot": want["hotspot"], "networks": want["networks"],
           "desired_channels": [{k: v for k, v in c.items() if k in ("name", "radio_tg", "network_tg", "slot", "label", "network", "sources")}
                                for c in want["channels"].values()],
           "before": before}
    if dry_run or before["in_sync"]:
        res["changed"] = False
        return res
    res["applied"] = sync.apply(doc, want, zone, remove_extra)
    cp.save(codeplug, doc)
    res["validation"] = await radio.encode_check(path)
    res["changed"] = True
    return res


async def wpsd_sync_status(codeplug: str = "", zone: str = sync.DEFAULT_ZONE) -> dict:
    """Read-only drift check between the live WPSD/BrandMeister setup and the codeplug's hotspot zone."""
    codeplug = codeplug or _default_codeplug()
    info = await sync.gather()
    want = sync.desired(info)
    if not codeplug:
        return {"codeplug": None, "hotspot": want["hotspot"], "in_sync": False, "note": "no codeplug in workspace yet"}
    return {"codeplug": codeplug, "hotspot": want["hotspot"], **sync.diff(cp.load_ro(codeplug), want, zone)}


async def _safe(coro):
    try:
        return {"ok": True, "data": await coro}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def dashboard_snapshot(codeplug: str = "", last_heard: int = 30) -> dict:
    """Everything the dashboard page needs in one read-only call: hotspot status, last heard (with TG names),
    BrandMeister device/profile, codeplug contents (zones -> channels, talkgroups, group lists), WPSD<->codeplug
    sync drift, and the latest backups. Each section has ok/error so one failure doesn't hide the rest."""
    import asyncio as _a
    codeplug = codeplug or _default_codeplug()
    info_r, lh_r = await _a.gather(_safe(sync.gather()), _safe(hotspot.last_heard(last_heard)))
    out = {"generated": datetime.now().isoformat(timespec="seconds"), "callsign": config.CALLSIGN,
           "dmr_id": config.DMR_ID, "codeplug_name": codeplug, "workspace": [w["name"] for w in cp.list_workspace()]}
    tgn = {}
    if info_r["ok"]:
        info = info_r["data"]
        tgn = info.get("tg_names") or {}
        st = info["status"]
        out["hotspot"] = {"ok": True, "data": {**st, "networks": sync.desired(info, include_defaults=False)["networks"],
                                               "bm_id": info["bm_id"]}}
        prof = info.get("profile") or {}
        subs = []
        for kind in ("staticSubscriptions", "dynamicSubscriptions", "timedSubscriptions"):
            for sbs in prof.get(kind, []) or []:
                tg = str(sbs.get("talkgroup"))
                subs.append({"type": kind.replace("Subscriptions", ""), "tg": tg, "slot": sbs.get("slot"),
                             "name": tgn.get(tg, "")})
        dev = {}
        if info["bm_id"]:
            dv = await _safe(data.bm_device(info["bm_id"]))
            if dv["ok"]:
                d = dv["data"]
                dev = {k: d.get(k) for k in ("id", "callsign", "hardware", "firmware", "statusText", "lastKnownMaster", "last_seen", "tx", "rx", "colorcode")}
        out["brandmeister"] = {"ok": True, "data": {"device": dev, "subscriptions": subs}}
    else:
        out["hotspot"] = out["brandmeister"] = info_r
    if lh_r["ok"]:
        for ev in lh_r["data"]:
            m = re.match(r"TG (\d+)", ev.get("to") or "")
            ev["tg_name"] = tgn.get(m.group(1), "") if m else ""
        out["last_heard"] = {"ok": True, "data": lh_r["data"]}
    else:
        out["last_heard"] = lh_r
    if codeplug:
        try:
            doc = cp.load_ro(codeplug)
            chans = {b.get("id"): (k, cp.summary_line("channels", k, b)) for k, b in cp.items(doc, "channels")} if doc.get("channels") else {}
            contacts = {b.get("id"): cp.summary_line("contacts", k, b) for k, b in cp.items(doc, "contacts")} if doc.get("contacts") else {}
            gls = [cp.summary_line("groupLists", None, b) for _, b in cp.items(doc, "groupLists")] if doc.get("groupLists") else []
            zones = []
            for _, z in cp.items(doc, "zones") if doc.get("zones") else []:
                zl = []
                for side in ("A", "B"):
                    for cid in z.get(side) or []:
                        if cid in chans:
                            k, sline = chans[cid]
                            c = contacts.get(sline.get("contact"))
                            zl.append({**sline, "kind": k, "side": side,
                                       "contact_name": c.get("name") if c else None,
                                       "contact_number": c.get("number") if c else None})
                zones.append({"id": z.get("id"), "name": z.get("name"), "channels": zl})
            in_zone = {c["id"] for z in zones for c in z["channels"]}
            loose = [{**s, "kind": k} for cid, (k, s) in chans.items() if cid not in in_zone]
            st = cp.path_for(codeplug).stat()
            out["codeplug"] = {"ok": True, "data": {"name": codeplug, "modified": int(st.st_mtime), "overview": cp.overview(doc),
                                                    "zones": zones, "channels_without_zone": loose,
                                                    "contacts": list(contacts.values()), "group_lists": gls}}
            if info_r["ok"]:
                out["sync"] = {"ok": True, "data": sync.diff(doc, sync.desired(info_r["data"]))}
        except Exception as e:  # noqa: BLE001
            out["codeplug"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        out["codeplug"] = {"ok": False, "error": "no codeplug in workspace yet (radio_read or codeplug_import_rdt)"}
    bk = {}
    if config.QDMR_BACKUP_DIR.exists():
        q = sorted(config.QDMR_BACKUP_DIR.rglob("*.yaml"))
        bk["last_radio_read"] = str(q[-1].relative_to(config.BACKUP_DIR)) if q else None
    if config.CPS_CODEPLUG_DIR.exists():
        r = sorted(p for p in config.CPS_CODEPLUG_DIR.rglob("D878UVII_*.rdt"))
        bk["last_cps_rdt"] = str(r[-1].relative_to(config.BACKUP_DIR)) if r else None
    out["backups"] = bk
    return out
