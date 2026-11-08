# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""AnyTone D578UV: RX group lists that vanish after a write.

The D578UV (firmware V111) stores which group-list slots are in use in a 32-byte bitmap at 0x025C0B10 and
programs it WITHOUT erasing the flash sector first. Flash bits only go 1 -> 0, so after every write the bitmap
is (old AND new). qdmr writes the lists into slots 0..n-1 and enables exactly those, so a slot that was ever
disabled stays disabled, and a list qdmr puts there is silently dropped by the radio (the channels that used it
end up with no RX list).

Work-around, done for the write only (the user's codeplug is never changed):
  * the bitmap is read alongside every radio read (overlay read, no extra session) and kept per radio;
  * before a write the real lists are laid out over the slots that are still enabled, with small "spare"
    lists in the disabled slots before them (qdmr numbers lists by position, so channels keep pointing at the
    right list);
  * a few spare lists after the real ones keep enabled slots alive for lists added later (writing fewer lists
    than the radio has enabled would disable the rest for good);
  * the spare lists are removed again from what Stentor shows after a read.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import local as ctx

MODELS = {"d578uv"}
BITMAP_ADDR = 0x025C0B10
BITMAP_LEN = 32
MAX_LISTS = 250
RESERVE = 8                        # enabled slots kept alive after the last real list
PAD_NAME = "STNTR SPARE {}"
PAD_RE = re.compile(r"^STNTR SPARE \d+$")
STATE_FILE = ".glist-bitmap.json"


def affected(model: str | None = None) -> bool:
    return (model if model is not None else ctx.radio_model()) in MODELS


def read_addresses() -> list[int]:
    return [BITMAP_ADDR + 16 * i for i in range(BITMAP_LEN // 16)]


def decode(blocks: dict[int, bytes]) -> list[int] | None:
    """Enabled slot numbers from the overlay-read blocks; None when a block is missing."""
    raw = b""
    for a in read_addresses():
        b = blocks.get(a)
        if b is None or len(b) != 16:
            return None
        raw += b
    return [i for i in range(MAX_LISTS) if raw[i // 8] >> (i % 8) & 1]


def _state_path() -> Path:
    return ctx.workspace() / STATE_FILE


def save_state(enabled: list[int], radio: str | None = None) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"enabled": enabled, "radio": radio, "read_at": round(time.time())}))
    tmp.replace(p)


def load_state() -> dict | None:
    try:
        st = json.loads(_state_path().read_text())
        return st if isinstance(st.get("enabled"), list) else None
    except (OSError, ValueError):
        return None


def plan(n_real: int, enabled: list[int]) -> dict:
    """Slot layout for n_real lists: 'R' real, 'P' spare. Lists that find no enabled slot are 'lost'."""
    en = set(int(s) for s in enabled if 0 <= int(s) < MAX_LISTS)
    order = sorted(en)
    layout: list[str] = []
    placed = 0
    for s in range(MAX_LISTS):
        if placed == n_real:
            break
        if s in en:
            layout.append("R")
            placed += 1
        else:
            layout.append("P")
    if placed < n_real:                        # ran out of slots
        layout = layout[:max(order) + 1] if order else []
    keep = min(len(order), placed + RESERVE)   # enabled slots to keep alive
    if keep:
        layout += ["P"] * max(0, order[keep - 1] + 1 - len(layout))
    while layout and layout[-1] == "P" and (len(layout) - 1) not in en:
        layout.pop()                           # a trailing spare in a disabled slot does nothing
    return {"layout": "".join(layout), "lists": n_real, "placed": placed, "lost": n_real - placed,
            "pads": layout.count("P"), "capacity": len(order)}


def _pad_contact(doc, real) -> str | None:
    for g in real:
        cs = list(g.get("contacts") or [])
        if cs:
            return str(cs[0])
    for c in doc.get("contacts") or []:
        body = next(iter(c.values())) if isinstance(c, dict) and len(c) == 1 else c
        if isinstance(body, dict) and body.get("id"):
            return str(body["id"])
    return None


def apply(doc, enabled: list[int]) -> dict:
    """Rewrite doc['groupLists'] in place for the slot layout. Returns the plan (+ 'applied')."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    real = list(doc.get("groupLists") or [])
    p = plan(len(real), enabled)
    contact = _pad_contact(doc, real)
    if p["pads"] == 0 or contact is None:
        p["applied"] = False
        return p
    used = {str(g.get("id")) for g in real}
    seq = CommentedSeq()
    it = iter(real)
    n = 0
    for kind in p["layout"]:
        if kind == "R":
            seq.append(next(it))
            continue
        n += 1
        gid = f"grpstntr{n}"
        while gid in used:
            gid += "x"
        m = CommentedMap()
        m["id"], m["name"] = gid, PAD_NAME.format(n)
        m["contacts"] = CommentedSeq([contact])
        m["contacts"].fa.set_flow_style()
        seq.append(m)
    seq.extend(it)                             # lost lists stay at the end (the radio drops them)
    doc["groupLists"] = seq
    p["applied"] = True
    return p


def prepare_write(src: Path, dst: Path, enabled: list[int]) -> dict:
    """Copy src to dst with the slot layout applied (dst is what dmrconf writes)."""
    from . import codeplug as cp
    with src.open() as f:
        doc = cp._yaml.load(f)
    p = apply(doc, enabled)
    if p["applied"]:
        dst.write_text(cp.dump_str(doc))
    return p


def strip_pads(path: Path) -> int:
    """Remove the spare lists Stentor wrote from a read codeplug (only unreferenced ones). Returns how many."""
    text = path.read_text()
    if "STNTR SPARE" not in text:
        return 0
    from . import codeplug as cp
    doc = cp._yaml.load(text)
    lists = doc.get("groupLists") or []
    pads = {str(g.get("id")) for g in lists if PAD_RE.match(str(g.get("name") or ""))}
    if not pads:
        return 0
    refs = set()
    for c in doc.get("channels") or []:
        body = next(iter(c.values())) if isinstance(c, dict) and len(c) == 1 else c
        if isinstance(body, dict) and body.get("groupList") is not None:
            refs.add(str(cp.plain(body.get("groupList"))))
    drop = pads - refs
    if not drop:
        return 0
    keep = [g for g in lists if str(g.get("id")) not in drop]
    lists[:] = keep
    path.write_text(cp.dump_str(doc))
    return len(drop)


def lint_issue(p: dict) -> dict | None:
    """A lint-style issue for the write preview when lists will not fit."""
    if not p or p.get("lost", 0) <= 0:
        return None
    cap, lost = p["capacity"], p["lost"]
    return {"level": "warn", "code": "d578_glist_slots",
            "msg": {"el": f"Ο D578 δέχεται αυτή τη στιγμή μόνο {cap} λίστες RX (θέσεις που έχει κλειδώσει το firmware). "
                          f"{lost} λίστα/ες δεν θα γραφτούν — ένωσε λίστες ή γράψε μία φορά με το CPS της AnyTone.",
                    "en": f"This D578 currently accepts only {cap} RX group lists (slots its firmware has locked). "
                          f"{lost} list(s) will not be written — merge lists, or write once with AnyTone's CPS."},
            "items": [], "count": lost}
