# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""WPSD hotspot -> codeplug synchronisation.

Builds the desired set of hotspot channels from the live WPSD configuration (frequencies, colour code,
duplex/slots, DMRGateway networks and TG rewrites) plus BrandMeister subscriptions, then reconciles a
managed zone in a codeplug. Managed channels are the ones in the sync zone whose name starts with 'HS '.
"""
import re

from . import codeplug as cp
from . import config, data, hotspot

PREFIX = "HS "
DEFAULT_ZONE = "WPSD Hotspot"
# always-useful BrandMeister targets (tg, slot, short name)
DEFAULT_EXTRAS = [(202, 1, "Greece"), (91, 1, "Worldwide"), (9990, 2, "Parrot"), (4000, 2, "Disconnect")]


def _rewrites(net: dict, key: str) -> list[tuple[int, int, int, int, int]]:
    out = []
    for k, v in net.items():
        if k.startswith(key):
            try:
                parts = [int(x) for x in v.split(",")]
            except ValueError:
                continue
            if len(parts) >= 5:
                out.append(tuple(parts[:5]))
            elif len(parts) == 4:
                out.append((*parts, 1))
    return out


def networks(gw_sections: dict) -> list[dict]:
    """Enabled DMRGateway networks with how a radio TG maps onto them."""
    nets = []
    for sec, kv in gw_sections.items():
        if not sec.startswith("DMR Network") or kv.get("Enabled") != "1":
            continue
        name = kv.get("Name", sec)
        passall = {int(v) for k, v in kv.items() if k.startswith("PassAllTG") and v.isdigit()}
        nets.append({
            "section": sec, "name": name, "address": kv.get("Address"), "id": kv.get("Id"),
            "is_brandmeister": "brandmeister" in str(kv.get("Address", "")).lower(),
            "pass_all_tg_slots": sorted(passall),
            "tg_rewrites": _rewrites(kv, "TGRewrite"),
        })
    return nets


def radio_tg_for(net: dict, tg: int, slot: int) -> int | None:
    """Which TG the radio must dial on `slot` to reach network TG `tg` (None = not reachable)."""
    for from_slot, from_tg, to_slot, to_tg, rng in net["tg_rewrites"]:
        if to_slot == slot and from_slot == slot and to_tg <= tg < to_tg + rng:
            return from_tg + (tg - to_tg)
    if slot in net["pass_all_tg_slots"]:
        return tg
    return None


async def gather() -> dict:
    st = await hotspot.status()
    gw = (await hotspot.read_config("dmrgateway"))["sections"]
    nets = networks(gw)
    bm_id = next((n["id"] for n in nets if n["is_brandmeister"]), None)
    profile = {}
    if bm_id:
        try:
            profile = await data.bm_device_profile(bm_id)
        except Exception:  # noqa: BLE001
            profile = {}
    try:
        tgnames = await data.bm_talkgroups()
    except Exception:  # noqa: BLE001
        tgnames = {}
    return {"status": st, "networks": nets, "bm_id": bm_id, "profile": profile, "tg_names": tgnames}


def desired(info: dict, extra_talkgroups: list[dict] | None = None, dmrplus_talkgroups: list[dict] | None = None,
            include_defaults: bool = True) -> dict:
    st = info["status"]
    duplex = str(st.get("duplex")) == "1"
    slots = [1, 2] if duplex else [2]
    # radio side is the reverse of the hotspot side
    radio_rx = st.get("tx_mhz")
    radio_tx = st.get("rx_mhz") if duplex else st.get("tx_mhz")
    cc = st.get("color_code") or 1
    nets = info["networks"]
    bm = next((n for n in nets if n["is_brandmeister"]), None)
    other = [n for n in nets if not n["is_brandmeister"]]
    tgnames = info.get("tg_names") or {}

    wanted: dict[tuple[int, int], dict] = {}

    def add(tg: int, slot: int, label: str, source: str, net: dict | None):
        if slot not in slots:
            slot = slots[-1]
        radio_tg = radio_tg_for(net, tg, slot) if net else tg
        if radio_tg is None:
            return
        key = (radio_tg, slot)
        if key in wanted:
            wanted[key]["sources"].append(source)
            return
        wanted[key] = {"radio_tg": radio_tg, "network_tg": tg, "slot": slot,
                       "label": (label or str(tgnames.get(str(tg), "")) or f"TG {tg}")[:16],
                       "network": net["name"] if net else "?", "sources": [source]}

    prof = info.get("profile") or {}
    for kind in ("staticSubscriptions", "dynamicSubscriptions", "timedSubscriptions"):
        for sub in prof.get(kind, []) or []:
            try:
                tg, slot = int(sub.get("talkgroup")), int(sub.get("slot") or 0)
            except (TypeError, ValueError):
                continue
            add(tg, slot or slots[-1], "", f"bm:{kind.replace('Subscriptions', '')}", bm)
    if include_defaults and bm:
        for tg, slot, label in DEFAULT_EXTRAS:
            add(tg, slot, label, "default", bm)
    for t in extra_talkgroups or []:
        add(int(t["tg"]), int(t.get("slot", slots[-1])), t.get("name", ""), "extra", bm)
    for t in dmrplus_talkgroups or []:
        if other:
            add(int(t["tg"]), int(t.get("slot", 1)), t.get("name", ""), "dmrplus", other[0])

    channels = {}
    for (rtg, slot), w in sorted(wanted.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        name = f"{PREFIX}{rtg} TS{slot}"[:16]
        channels[name] = {**w, "name": name, "rx_mhz": radio_rx, "tx_mhz": radio_tx, "color_code": cc}
    return {"hotspot": {"rx_mhz": radio_rx, "tx_mhz": radio_tx, "color_code": cc, "duplex": duplex, "slots": slots,
                        "callsign": st.get("callsign"), "dmr_id": st.get("dmr_id")},
            "networks": [{"name": n["name"], "brandmeister": n["is_brandmeister"],
                          "pass_all_tg_slots": n["pass_all_tg_slots"], "rewrites": len(n["tg_rewrites"])} for n in nets],
            "channels": channels}


def _mhz_num(v) -> float | None:
    m = re.match(r"\s*([\d.]+)", str(v or ""))
    return round(float(m.group(1)), 5) if m else None


def diff(doc, want: dict, zone_name: str = DEFAULT_ZONE) -> dict:
    zid = cp.resolve(doc, "zones", zone_name)
    zone_ch = []
    if zid:
        _, z = cp.find(doc, "zones", zid)
        zone_ch = list(z.get("A") or []) + list(z.get("B") or [])
    existing = {}
    for cid in zone_ch:
        kind, body = cp.find(doc, "channels", cid)
        if body is not None and str(body.get("name", "")).startswith(PREFIX):
            existing[str(body.get("name"))] = (cid, kind, body)
    contacts = {b.get("id"): b for _, b in cp.items(doc, "contacts")} if doc.get("contacts") else {}
    missing, outdated, ok = [], [], []
    for name, w in want["channels"].items():
        if name not in existing:
            missing.append(name)
            continue
        cid, kind, b = existing[name]
        problems = []
        if kind != "dmr":
            problems.append("not a DMR channel")
        if _mhz_num(b.get("rxFrequency")) != _mhz_num(w["rx_mhz"]):
            problems.append(f"rx {b.get('rxFrequency')} -> {w['rx_mhz']} MHz")
        if _mhz_num(b.get("txFrequency")) != _mhz_num(w["tx_mhz"]):
            problems.append(f"tx {b.get('txFrequency')} -> {w['tx_mhz']} MHz")
        if int(b.get("colorCode") or 0) != int(w["color_code"]):
            problems.append(f"cc {b.get('colorCode')} -> {w['color_code']}")
        if str(b.get("timeSlot")) != f"TS{w['slot']}":
            problems.append(f"slot {b.get('timeSlot')} -> TS{w['slot']}")
        c = contacts.get(b.get("contact"))
        if not c or str(c.get("number")) != str(w["radio_tg"]):
            problems.append(f"contact -> TG {w['radio_tg']}")
        (outdated if problems else ok).append({"name": name, "id": cid, "problems": problems} if problems else name)
    extra = [n for n in existing if n not in want["channels"]]
    return {"zone": zone_name, "zone_exists": bool(zid), "in_sync": not (missing or outdated or extra),
            "missing": missing, "outdated": outdated, "extra": extra, "ok": ok}


def apply(doc, want: dict, zone_name: str = DEFAULT_ZONE, remove_extra: bool = True) -> dict:
    hs = want["hotspot"]
    # radio ID
    rid = None
    if hs.get("dmr_id"):
        rid = next((b.get("id") for _, b in cp.items(doc, "radioIDs") if doc.get("radioIDs")
                    and str(b.get("number")) == str(hs["dmr_id"])), None)
        if not rid:
            rid = cp.upsert(doc, "radioIDs", {"name": hs.get("callsign") or config.CALLSIGN, "number": int(hs["dmr_id"])}, "dmr")
    # group lists per slot
    gl_ids = {}
    for slot in hs["slots"]:
        gname = f"WPSD TS{slot}"
        gl_ids[slot] = cp.resolve(doc, "groupLists", gname) or cp.upsert(doc, "groupLists", {"name": gname, "contacts": []})
    zid = cp.resolve(doc, "zones", zone_name) or cp.upsert(doc, "zones", {"name": zone_name[:16], "A": [], "B": []})
    _, zone = cp.find(doc, "zones", zid)
    before = diff(doc, want, zone_name)
    created, updated, removed = [], [], []
    existing_by_name = {}
    for cid in list(zone.get("A") or []):
        _, b = cp.find(doc, "channels", cid)
        if b is not None and str(b.get("name", "")).startswith(PREFIX):
            existing_by_name[str(b.get("name"))] = cid
    order = []
    for name, w in want["channels"].items():
        # contact
        cont = next((b.get("id") for _, b in cp.items(doc, "contacts") if doc.get("contacts")
                     and str(b.get("number")) == str(w["radio_tg"]) and b.get("type") == "GroupCall"), None)
        if not cont:
            cont = cp.upsert(doc, "contacts", {"name": w["label"] or f"TG {w['radio_tg']}", "ring": False,
                                               "type": "GroupCall", "number": int(w["radio_tg"])}, "dmr")
        _, gl = cp.find(doc, "groupLists", gl_ids[w["slot"]])
        if cont not in gl["contacts"]:
            gl["contacts"].append(cont)
        spec = {"name": name, "rxFrequency": w["rx_mhz"], "txFrequency": w["tx_mhz"], "colorCode": int(w["color_code"]),
                "timeSlot": f"TS{w['slot']}", "contact": cont, "groupList": gl_ids[w["slot"]], "admit": "Always", "power": "Low",  # hotspots do not answer the AnyTone wake-up that ColorCode/ChannelFree admit triggers ("Repeater not found")
                "rxOnly": False}
        if rid:
            spec["radioId"] = rid
        if name in existing_by_name:
            spec["id"] = existing_by_name[name]
            cp.upsert(doc, "channels", spec, "dmr")
            if name in [o["name"] for o in before["outdated"]]:
                updated.append(name)
            cid = spec["id"]
        else:
            cid = cp.upsert(doc, "channels", spec, "dmr")
            created.append(name)
        order.append(cid)
    if remove_extra:
        extra_ids = [existing_by_name[n] for n in existing_by_name if n not in want["channels"]]
        if extra_ids:
            removed = cp.delete(doc, "channels", extra_ids)["removed"]
    # zone = managed channels (sorted) + any non-managed channels the user put there, kept at the end
    keep = [c for c in (zone.get("A") or []) if c not in order and c not in removed]
    zone["A"] = cp.to_ruamel(order + keep)
    return {"zone": zid, "created": created, "updated": updated, "removed": removed,
            "unchanged": len(want["channels"]) - len(created) - len(updated)}
