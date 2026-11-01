# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""AnyTone CPS interchange: export a qdmr YAML codeplug as the CSV set the AnyTone CPS (D878UV/D878UVII/D578UV,
CPS 3.0x) imports table by table (Tool → Import), and keep the .rdt import (dmrconf manufacturer decoder) next to it.

The CPS is picky about headers: these are the column sets of CPS 3.0x for the D878UVII Plus. Other CPS versions
differ by a few trailing columns; the CPS reports the first column it does not know, which is the easiest way to
align. Everything qdmr does not model is written with the CPS defaults."""
import csv
import io
import zipfile

from . import codeplug as cp

CHANNEL_COLS = ["No.", "Channel Name", "Receive Frequency", "Transmit Frequency", "Channel Type", "Transmit Power",
                "Band Width", "CTCSS/DCS Decode", "CTCSS/DCS Encode", "Contact", "Contact Call Type",
                "Contact TG/DMR ID", "Radio ID", "Busy Lock/TX Permit", "Squelch Mode", "Optional Signal", "DTMF ID",
                "2Tone ID", "5Tone ID", "PTT ID", "Color Code", "Slot", "Scan List", "Receive Group List",
                "PTT Prohibit", "Reverse", "Simplex TDMA", "Slot Suit", "AES Digital Encryption",
                "Digital Encryption", "Call Confirmation", "Talk Around(Simplex)", "Work Alone", "Custom CTCSS",
                "2TONE Decode", "Ranging", "Through Mode", "APRS RX", "Analog APRS PTT Mode",
                "Digital APRS PTT Mode", "APRS Report Type", "Digital APRS Report Channel", "Correct Frequency[Hz]",
                "SMS Confirmation", "Exclude channel from roaming", "DMR MODE", "DataACK Disable", "R5toneBot",
                "R5ToneEot", "Auto Scan", "Ana Aprs Mute", "Send Talker Alias", "AnaAprsTxPath", "ARC4",
                "ex_emg_kind"]
TALKGROUP_COLS = ["No.", "Radio ID", "Name", "Call Type", "Call Alert"]
RADIOID_COLS = ["No.", "Radio ID", "Name"]
ZONE_COLS = ["No.", "Zone Name", "Zone Channel Member", "Zone Channel Member RX Frequency",
             "Zone Channel Member TX Frequency", "A Channel", "A Channel RX Frequency", "A Channel TX Frequency",
             "B Channel", "B Channel RX Frequency", "B Channel TX Frequency", "Zone Hide "]
SCANLIST_COLS = ["No.", "Scan List Name", "Scan Channel Member", "Scan Channel Member RX Frequency",
                 "Scan Channel Member TX Frequency", "Scan Mode", "Priority Channel Select", "Priority Channel 1",
                 "Priority Channel 1 RX Frequency", "Priority Channel 1 TX Frequency", "Priority Channel 2",
                 "Priority Channel 2 RX Frequency", "Priority Channel 2 TX Frequency", "Revert Channel",
                 "Look Back Time A[s]", "Look Back Time B[s]", "Dropout Delay Time[s]", "Dwell Time[s]"]
GROUPLIST_COLS = ["No.", "Group Name", "Contact", "Contact TG/DMR ID"]
ROAMING_CH_COLS = ["No.", "Receive Frequency", "Transmit Frequency", "Color Code", "Slot", "Name"]
ROAMING_ZONE_COLS = ["No.", "Name", "Roaming Channel Member"]

POWER = {"Min": "Low", "Low": "Low", "Mid": "Mid", "High": "High", "Max": "Turbo"}
ADMIT_DMR = {"Always": "Always", "Free": "ChannelFree", "ColorCode": "Different Color Code"}
ADMIT_FM = {"Always": "Always", "Free": "ChannelFree", "Tone": "ChannelFree"}
CALL_TYPE = {"GroupCall": "Group Call", "PrivateCall": "Private Call", "AllCall": "All Call"}


def _mhz(v) -> str:
    """'438.4 MHz' / 438.4 -> '438.40000' (the CPS wants five decimals)."""
    s = str(v).replace("MHz", "").strip()
    try:
        return f"{float(s):.5f}"
    except ValueError:
        return s


def _tone(t) -> str:
    """qdmr tone dict -> CPS text: 'Off', '88.5', 'D023N', 'D023I'."""
    if not t:
        return "Off"
    t = cp.plain(t)
    if isinstance(t, dict):
        if "ctcss" in t:
            return f"{float(str(t['ctcss']).replace('Hz', '').strip()):g}"
        if "dcs" in t:
            code = int(t["dcs"])
            return f"D{abs(code):03d}{'I' if code < 0 else 'N'}"
    return "Off"


def _names(doc) -> dict[str, dict[str, str]]:
    """id -> name for every referenced section."""
    out: dict[str, dict[str, str]] = {}
    for section in ("contacts", "radioIDs", "groupLists", "scanLists", "channels", "roamingChannels"):
        out[section] = {}
        for kind, body in cp.items(doc, section):
            if body.get("id"):
                out[section][body["id"]] = str(body.get("name", body["id"]))
    return out


def _channel_freqs(doc) -> dict[str, tuple[str, str]]:
    return {b["id"]: (_mhz(b.get("rxFrequency")), _mhz(b.get("txFrequency")))
            for _, b in cp.items(doc, "channels") if b.get("id")}


def _dmr_mode(rx: str, tx: str, dcdm: bool) -> str:
    if dcdm:
        return "2"
    return "0" if rx == tx else "1"


def channels_csv(doc) -> str:
    names = _names(doc)
    contacts = {b["id"]: b for _, b in cp.items(doc, "contacts") if b.get("id")}
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(CHANNEL_COLS)
    n = 0
    for kind, b in cp.items(doc, "channels"):
        n += 1
        rx, tx = _mhz(b.get("rxFrequency")), _mhz(b.get("txFrequency", b.get("rxFrequency")))
        digital = kind == "dmr"
        row = {c: "" for c in CHANNEL_COLS}
        row.update({
            "No.": n, "Channel Name": str(b.get("name", ""))[:16], "Receive Frequency": rx, "Transmit Frequency": tx,
            "Channel Type": "D-Digital" if digital else "A-Analog",
            "Transmit Power": POWER.get(str(b.get("power", "High")), "High"),
            "Band Width": "25K" if (not digital and str(b.get("bandwidth", "Narrow")) == "Wide") else "12.5K",
            "CTCSS/DCS Decode": _tone(b.get("rxTone")) if not digital else "Off",
            "CTCSS/DCS Encode": _tone(b.get("txTone")) if not digital else "Off",
            "Contact": "", "Contact Call Type": "Group Call", "Contact TG/DMR ID": "",
            "Radio ID": "", "Busy Lock/TX Permit": "Off", "Squelch Mode": "Carrier", "Optional Signal": "Off",
            "DTMF ID": "1", "2Tone ID": "1", "5Tone ID": "1", "PTT ID": "Off", "Color Code": "1", "Slot": "1",
            "Scan List": "None", "Receive Group List": "None", "PTT Prohibit": "On" if b.get("rxOnly") else "Off",
            "Reverse": "Off", "Simplex TDMA": "Off", "Slot Suit": "Off", "AES Digital Encryption": "Normal Encryption",
            "Digital Encryption": "Off", "Call Confirmation": "Off", "Talk Around(Simplex)": "Off", "Work Alone": "Off",
            "Custom CTCSS": "251.1", "2TONE Decode": "0", "Ranging": "Off", "Through Mode": "Off", "APRS RX": "Off",
            "Analog APRS PTT Mode": "Off", "Digital APRS PTT Mode": "Off", "APRS Report Type": "Off",
            "Digital APRS Report Channel": "1", "Correct Frequency[Hz]": "0", "SMS Confirmation": "Off",
            "Exclude channel from roaming": "0", "DMR MODE": "0", "DataACK Disable": "0", "R5toneBot": "0",
            "R5ToneEot": "0", "Auto Scan": "0", "Ana Aprs Mute": "0", "Send Talker Alias": "0", "AnaAprsTxPath": "0",
            "ARC4": "0", "ex_emg_kind": "0"})
        if b.get("scanList"):
            row["Scan List"] = names["scanLists"].get(b["scanList"], "None")
        if digital:
            c = contacts.get(b.get("contact") or "")
            if c:
                row["Contact"] = str(c.get("name", ""))
                row["Contact Call Type"] = CALL_TYPE.get(str(c.get("type", "GroupCall")), "Group Call")
                row["Contact TG/DMR ID"] = str(c.get("number", ""))
            if b.get("radioId"):
                row["Radio ID"] = names["radioIDs"].get(b["radioId"], "")
            elif names["radioIDs"]:
                row["Radio ID"] = next(iter(names["radioIDs"].values()))
            row["Busy Lock/TX Permit"] = ADMIT_DMR.get(str(b.get("admit", "Always")), "Always")
            row["Color Code"] = str(b.get("colorCode", 1))
            row["Slot"] = "2" if str(b.get("timeSlot", "TS1")) == "TS2" else "1"
            if b.get("groupList"):
                row["Receive Group List"] = names["groupLists"].get(b["groupList"], "None")
            ext = cp.plain(b.get("extended") or {})
            dcdm = bool(ext.get("dcdm")) if isinstance(ext, dict) else False
            row["DMR MODE"] = _dmr_mode(rx, tx, dcdm)
            if dcdm:
                row["Simplex TDMA"] = "On"
        else:
            row["Busy Lock/TX Permit"] = ADMIT_FM.get(str(b.get("admit", "Always")), "Always")
            if b.get("rxTone"):
                row["Squelch Mode"] = "CTCSS/DCS"
        w.writerow([row[c] for c in CHANNEL_COLS])
    return buf.getvalue()


def _simple_csv(cols: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def talkgroups_csv(doc) -> str:
    rows = []
    for i, (_, b) in enumerate(cp.items(doc, "contacts"), 1):
        if "number" not in b:
            continue                                   # DTMF / analog contacts have no DMR number
        rows.append([i, b.get("number"), str(b.get("name", ""))[:16], CALL_TYPE.get(str(b.get("type", "GroupCall")), "Group Call"),
                     "Online Alert" if b.get("ring") else "None"])
    return _simple_csv(TALKGROUP_COLS, rows)


def radioids_csv(doc) -> str:
    rows = [[i, b.get("number"), str(b.get("name", ""))[:16]] for i, (_, b) in enumerate(cp.items(doc, "radioIDs"), 1)]
    return _simple_csv(RADIOID_COLS, rows)


def _members(ids: list, names: dict, freqs: dict) -> tuple[str, str, str]:
    ids = [i for i in ids if i in names]
    return ("|".join(names[i] for i in ids), "|".join(freqs.get(i, ("", ""))[0] for i in ids),
            "|".join(freqs.get(i, ("", ""))[1] for i in ids))


def zones_csv(doc) -> str:
    names, freqs = _names(doc)["channels"], _channel_freqs(doc)
    rows = []
    for i, (_, z) in enumerate(cp.items(doc, "zones"), 1):
        a = list(z.get("A") or []) + list(z.get("B") or [])
        if not a:
            continue
        mem, rxs, txs = _members(a, names, freqs)
        first = next((c for c in a if c in names), None)
        bsel = next((c for c in (z.get("B") or []) if c in names), first)
        rows.append([i, str(z.get("name", ""))[:16], mem, rxs, txs,
                     names.get(first, ""), *(freqs.get(first, ("", ""))),
                     names.get(bsel, ""), *(freqs.get(bsel, ("", ""))), "0"])
    return _simple_csv(ZONE_COLS, rows)


def scanlists_csv(doc) -> str:
    names, freqs = _names(doc)["channels"], _channel_freqs(doc)
    rows = []
    for i, (_, s) in enumerate(cp.items(doc, "scanLists"), 1):
        chans = list(s.get("channels") or [])
        mem, rxs, txs = _members(chans, names, freqs)
        p1, p2 = s.get("primary"), s.get("secondary")
        sel = "Off"
        if p1 and p2:
            sel = "Priority Channel Select1 + Priority Channel Select2"
        elif p1:
            sel = "Priority Channel Select1"
        rows.append([i, str(s.get("name", ""))[:16], mem, rxs, txs, "Off", sel,
                     names.get(p1, "Off") if p1 else "Off", *(freqs.get(p1, ("", "")) if p1 else ("", "")),
                     names.get(p2, "Off") if p2 else "Off", *(freqs.get(p2, ("", "")) if p2 else ("", "")),
                     "Selected", "2", "3", "3.1", "0.1"])
    return _simple_csv(SCANLIST_COLS, rows)


def grouplists_csv(doc) -> str:
    contacts = {b["id"]: b for _, b in cp.items(doc, "contacts") if b.get("id")}
    rows = []
    for i, (_, g) in enumerate(cp.items(doc, "groupLists"), 1):
        cs = [contacts[c] for c in (g.get("contacts") or []) if c in contacts]
        rows.append([i, str(g.get("name", ""))[:16], "|".join(str(c.get("name", "")) for c in cs),
                     "|".join(str(c.get("number", "")) for c in cs)])
    return _simple_csv(GROUPLIST_COLS, rows)


def roaming_channels_csv(doc) -> str:
    rows = []
    for i, (_, r) in enumerate(cp.items(doc, "roamingChannels"), 1):
        cc = r.get("colorCode")
        ts = str(r.get("timeSlot", ""))
        rows.append([i, _mhz(r.get("rxFrequency")), _mhz(r.get("txFrequency")),
                     str(cc) if isinstance(cc, int) else "1", "2" if ts == "TS2" else "1", str(r.get("name", ""))[:16]])
    return _simple_csv(ROAMING_CH_COLS, rows)


def roaming_zones_csv(doc) -> str:
    names = _names(doc)["roamingChannels"]
    rows = [[i, str(z.get("name", ""))[:16], "|".join(names[c] for c in (z.get("channels") or []) if c in names)]
            for i, (_, z) in enumerate(cp.items(doc, "roamingZones"), 1)]
    return _simple_csv(ROAMING_ZONE_COLS, rows)


FILES = {"Channel.CSV": channels_csv, "TalkGroups.CSV": talkgroups_csv, "RadioIDList.CSV": radioids_csv,
         "Zone.CSV": zones_csv, "ScanList.CSV": scanlists_csv, "ReceiveGroupCallList.CSV": grouplists_csv,
         "RoamingChannel.CSV": roaming_channels_csv, "RoamingZone.CSV": roaming_zones_csv}


def export_csv(doc) -> dict[str, str]:
    return {name: fn(doc) for name, fn in FILES.items()}


def export_zip(doc) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in export_csv(doc).items():
            z.writestr(name, text.encode("utf-8-sig"))
        z.writestr("README.txt", "AnyTone CPS import order: RadioIDList, TalkGroups, ReceiveGroupCallList, Channel, "
                   "Zone, ScanList, RoamingChannel, RoamingZone (Tool -> Import, one file per table).\r\n"
                   "Headers follow CPS 3.0x for the D878UVII Plus; if your CPS names a column it does not know, "
                   "send that message with the export so the columns can be aligned.\r\n")
    return buf.getvalue()
