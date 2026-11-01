# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""qdmr extensible-YAML codeplug model helpers (round-trip safe via ruamel.yaml)."""
import difflib
import io
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import config

# sections whose items are wrapped as {kind: {...}}
WRAPPED = {"channels": ("fm", "dmr", "am"), "contacts": ("dmr", "dtmf"),
           "positioning": ("dmr", "aprs"), "radioIDs": ("dmr",), "roamingChannels": None}
FLAT = ("groupLists", "zones", "scanLists", "roamingZones")
ID_PREFIX = {"channels": "ch", "contacts": "cont", "groupLists": "grp", "zones": "zone",
             "scanLists": "scan", "positioning": "aprs", "radioIDs": "id", "roamingZones": "rz",
             "roamingChannels": "rc"}
SECTIONS = tuple(WRAPPED) + FLAT

_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.explicit_start = True
_yaml.representer.add_representer(type(None), lambda r, d: r.represent_scalar("tag:yaml.org,2002:null", "~"))
_LOCAL_TAG = re.compile(r"(?<=[:\[,\-] )!([A-Za-z]\w*)(?= )")


# ---------------------------------------------------------------- files
def safe_name(name: str) -> str:
    n = re.sub(r"\.ya?ml$", "", name.strip())
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", n) or n.startswith("."):
        raise ValueError(f"Invalid codeplug name '{name}' (use letters, digits, _ . -)")
    return n


def path_for(name: str) -> Path:
    return config.WORKSPACE / f"{safe_name(name)}.yaml"


# ---------------------------------------------------------------- document cache
# Parsing/dumping a 1 MB codeplug takes seconds, so documents live in memory and are written back to disk
# in a background thread shortly after the last edit. flush_all() forces pending writes (call it before
# anything that reads the files directly: dmrconf, copies, diffs).
import atexit
import threading

_lock = threading.RLock()
_docs: dict[str, dict] = {}          # path -> {doc, mtime_ns, dirty, timer}
_FLUSH_DELAY = 0.8


def _entry(p: Path) -> dict | None:
    e = _docs.get(str(p))
    if e is None:
        return None
    if not e["dirty"] and (not p.exists() or p.stat().st_mtime_ns != e["mtime_ns"]):
        return None  # changed on disk (external write) -> reparse
    return e


def load(name: str) -> CommentedMap:
    p = path_for(name)
    with _lock:
        e = _entry(p)
        if e is not None:
            return e["doc"]
        if not p.exists():
            raise FileNotFoundError(f"Codeplug '{name}' not found in workspace. Use codeplug_list.")
        with p.open() as f:
            doc = _yaml.load(f)
        if len(_docs) > 6:
            for k in [k for k, v in _docs.items() if not v["dirty"]][:3]:
                _docs.pop(k, None)
        _docs[str(p)] = {"doc": doc, "mtime_ns": p.stat().st_mtime_ns, "dirty": False, "timer": None}
        return doc


load_ro = load


def _write(p: Path) -> None:
    with _lock:
        e = _docs.get(str(p))
        if e is None or not e["dirty"]:
            return
        text = dump_str(e["doc"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(p)
        e["dirty"] = False
        e["mtime_ns"] = p.stat().st_mtime_ns
        e["timer"] = None


def save(name: str, doc) -> Path:
    """Register `doc` as the current content of `name` and schedule the disk write."""
    p = path_for(name)
    with _lock:
        e = _docs.get(str(p))
        if e is None or e["doc"] is not doc:
            e = {"doc": doc, "mtime_ns": 0, "dirty": True, "timer": None}
            _docs[str(p)] = e
        e["dirty"] = True
        if e["timer"]:
            e["timer"].cancel()
        t = threading.Timer(_FLUSH_DELAY, _write, args=(p,))
        t.daemon = True
        e["timer"] = t
        t.start()
    return p


def flush(name: str) -> None:
    p = path_for(name)
    with _lock:
        e = _docs.get(str(p))
        if e and e["timer"]:
            e["timer"].cancel()
    _write(p)


def flush_all() -> None:
    with _lock:
        paths = [Path(k) for k, v in _docs.items() if v["dirty"]]
        for v in _docs.values():
            if v["timer"]:
                v["timer"].cancel()
    for p in paths:
        _write(p)


def forget(name: str) -> None:
    """Drop the cached document (after an external overwrite such as codeplug_copy)."""
    with _lock:
        _docs.pop(str(path_for(name)), None)


atexit.register(flush_all)


def dump_str(doc) -> str:
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    # qdmr writes verbatim tags (!<!default>); keep that spelling
    return _LOCAL_TAG.sub(r"!<!\1>", buf.getvalue())


_norm_cache: dict[str, tuple[int, str]] = {}


def normalized(path: Path) -> str:
    """Load+dump so that pure formatting differences disappear in diffs. Cached per file mtime (a 1 MB codeplug
    takes ~5 s to round-trip); a document already in the editor cache is dumped from memory."""
    flush_all()
    key = str(path)
    mt = path.stat().st_mtime_ns
    hit = _norm_cache.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    with _lock:
        e = _docs.get(key)
        doc = e["doc"] if e and not e["dirty"] and e["mtime_ns"] == mt else None
    if doc is None:
        with path.open() as f:
            doc = _yaml.load(f)
    text = dump_str(doc)
    if len(_norm_cache) > 8:
        _norm_cache.pop(next(iter(_norm_cache)))
    _norm_cache[key] = (mt, text)
    return text




def list_workspace() -> list[dict]:
    flush_all()
    out = []
    for p in sorted(config.WORKSPACE.glob("*.yaml")):
        st = p.stat()
        out.append({"name": p.stem, "size": st.st_size, "modified": int(st.st_mtime)})
    return out


# ---------------------------------------------------------------- plain conversion
def plain(v: Any) -> Any:
    """ruamel objects -> JSON-friendly python (tagged scalars like !<!default> become '!default')."""
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    tag = getattr(getattr(v, "tag", None), "value", None)
    if tag and str(tag).startswith("!"):
        return str(tag).replace("!<", "").replace(">", "") or "!tag"
    if hasattr(v, "value") and not isinstance(v, (str, int, float, bool)):
        return plain(v.value)
    return v


def to_ruamel(v: Any) -> Any:
    if isinstance(v, dict):
        m = CommentedMap()
        for k, x in v.items():
            m[k] = to_ruamel(x)
        return m
    if isinstance(v, list):
        s = CommentedSeq([to_ruamel(x) for x in v])
        if all(not isinstance(x, (dict, list)) for x in v):
            s.fa.set_flow_style()
        return s
    return v


# ---------------------------------------------------------------- item access
def items(doc, section: str) -> list[tuple[str | None, CommentedMap]]:
    """Return [(kind, body)] for a section."""
    if section not in SECTIONS:
        raise ValueError(f"Unknown section '{section}'. Valid: {', '.join(SECTIONS)}")
    seq = doc.get(section) or []
    res = []
    for it in seq:
        if section in FLAT or section == "roamingChannels":
            res.append((None, it))
        else:
            kind = next(iter(it))
            res.append((kind, it[kind]))
    return res


def find(doc, section: str, ident: str):
    for kind, body in items(doc, section):
        if body.get("id") == ident:
            return kind, body
    return None, None


def find_by_name(doc, section: str, name: str):
    for kind, body in items(doc, section):
        if str(body.get("name", "")).lower() == name.lower():
            return kind, body
    return None, None


def resolve(doc, section: str, ref: str) -> str | None:
    """Accept an id or a (case-insensitive) name, return the id."""
    if ref is None or ref == "":
        return None
    _, body = find(doc, section, ref)
    if body is None:
        _, body = find_by_name(doc, section, ref)
    return body.get("id") if body is not None else None


def next_id(doc, section: str) -> str:
    prefix = ID_PREFIX.get(section, "x")
    nums = [0]
    for _, body in items(doc, section):
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", str(body.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    return f"{prefix}{max(nums) + 1}"


def summary_line(section: str, kind, body) -> dict:
    b = plain(body)
    s = {"id": b.get("id"), "name": b.get("name")}
    if kind:
        s["kind"] = kind
    if section == "channels":
        for k in ("rxFrequency", "txFrequency", "colorCode", "timeSlot", "contact", "groupList", "rxTone", "txTone", "power", "bandwidth"):
            if k in b and b[k] not in (None, "", "!default"):
                s[k] = b[k]
    elif section == "contacts":
        s.update({k: b.get(k) for k in ("type", "number") if k in b})
    elif section in ("groupLists",):
        s["contacts"] = b.get("contacts", [])
    elif section == "zones":
        s["A"] = b.get("A", [])
        if b.get("B"):
            s["B"] = b["B"]
    elif section == "scanLists":
        s["channels"] = b.get("channels", [])
    elif section == "radioIDs":
        s["number"] = b.get("number")
    return s


def overview(doc) -> dict:
    o = {"version": plain(doc.get("version")), "counts": {}}
    for sec in SECTIONS:
        if doc.get(sec) is not None:
            o["counts"][sec] = len(doc.get(sec) or [])
    o["radioIDs"] = [summary_line("radioIDs", k, b) for k, b in items(doc, "radioIDs")] if doc.get("radioIDs") else []
    st = plain(doc.get("settings") or {})
    o["settings"] = {k: st.get(k) for k in ("introLine1", "introLine2", "defaultID", "power", "squelch", "micLevel") if k in st}
    o["zones"] = [{"id": b.get("id"), "name": b.get("name"), "channels": len(b.get("A") or []) + len(b.get("B") or [])}
                  for _, b in items(doc, "zones")] if doc.get("zones") else []
    return o


# ---------------------------------------------------------------- mutation helpers
def mhz(v) -> str:
    if v is None or v == "":
        return v
    if isinstance(v, (int, float)):
        return f"{float(v):g} MHz"
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return f"{float(s):g} MHz"
    return s


def ensure_section(doc, section: str):
    if doc.get(section) is None:
        doc[section] = CommentedSeq()
    return doc[section]


def upsert(doc, section: str, entry: dict, kind: str | None = None) -> str:
    """Insert or merge an entry. Returns its id."""
    entry = dict(entry)
    for f in ("rxFrequency", "txFrequency"):
        if f in entry:
            entry[f] = mhz(entry[f])
    ident = entry.get("id")
    if ident:
        k, body = find(doc, section, ident)
        if body is not None:
            for key, val in entry.items():
                if val is None and key in body:
                    del body[key]
                else:
                    body[key] = to_ruamel(val)
            return ident
    ident = ident or next_id(doc, section)
    entry["id"] = ident
    # keep id first, drop unset fields (qdmr defaults apply)
    ordered = {"id": ident, **{k: v for k, v in entry.items() if k != "id" and v is not None}}
    seq = ensure_section(doc, section)
    body = to_ruamel(ordered)
    if section in ("contacts", "groupLists", "zones", "radioIDs", "scanLists"):
        body.fa.set_flow_style()
    if section in WRAPPED and section != "roamingChannels":
        allowed = WRAPPED[section]
        kind = kind or allowed[0]
        if allowed and kind not in allowed:
            raise ValueError(f"kind '{kind}' invalid for {section}; use one of {allowed}")
        wrapper = CommentedMap()
        wrapper[kind] = body
        seq.append(wrapper)
    else:
        seq.append(body)
    return ident


def _scrub_refs(doc, ident: str) -> list[str]:
    touched = []
    for sec, field in (("zones", "A"), ("zones", "B"), ("groupLists", "contacts"), ("scanLists", "channels"),
                       ("roamingZones", "channels")):
        for _, body in items(doc, sec) if doc.get(sec) else []:
            lst = body.get(field)
            if lst and ident in lst:
                while ident in lst:
                    lst.remove(ident)
                touched.append(f"{sec}:{body.get('id')}.{field}")
    for _, body in items(doc, "channels") if doc.get("channels") else []:
        for field in ("contact", "groupList", "aprs", "scanList", "roaming", "radioId"):
            if body.get(field) == ident:
                body[field] = None
                touched.append(f"channels:{body.get('id')}.{field}")
    return touched


def delete(doc, section: str, ids: list[str]) -> dict:
    seq = doc.get(section) or []
    removed, refs = [], []
    for ident in ids:
        for i, it in enumerate(list(seq)):
            body = it if (section in FLAT or section == "roamingChannels") else it[next(iter(it))]
            if body.get("id") == ident:
                del seq[i]
                removed.append(ident)
                refs += _scrub_refs(doc, ident)
                break
    return {"removed": removed, "not_found": [i for i in ids if i not in removed], "references_cleared": refs}


def set_path(doc, dotted: str, value):
    """Set a nested settings value, e.g. 'settings.introLine1'."""
    parts = dotted.split(".")
    cur = doc
    for p in parts[:-1]:
        if cur.get(p) is None:
            cur[p] = CommentedMap()
        cur = cur[p]
    old = plain(cur.get(parts[-1]))
    cur[parts[-1]] = to_ruamel(value)
    return old


def get_path(doc, dotted: str):
    cur = doc
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(dotted)
        cur = cur[p]
    return plain(cur)


def diff_text(a: str, b: str, name_a: str, name_b: str, context: int = 2) -> str:
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), name_a, name_b, n=context))


def new_minimal(callsign: str, dmr_id: int, intro2: str = "") -> CommentedMap:
    return to_ruamel({
        "version": "0.14.0",
        "settings": {"introLine1": callsign, "introLine2": intro2 or "D878UVII", "micLevel": 3, "speech": False,
                     "power": "High", "squelch": 1, "vox": "off", "defaultID": "id1"},
        "radioIDs": [{"dmr": {"id": "id1", "name": callsign, "number": int(dmr_id)}}],
        "contacts": [], "groupLists": [], "channels": [], "zones": [],
    })
