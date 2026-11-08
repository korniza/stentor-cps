# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Exercise the editor's API path exactly as shack.html does, on a scratch copy of current."""
import httpx, json
H = {"X-Shack": "1"}
B = "http://127.0.0.1:8000/ui/api/tool/"
def t(name, body): r = httpx.post(B + name, json=body, headers=H, timeout=120); j = r.json(); assert r.status_code == 200, (name, j); return j
t("codeplug_copy", {"source": "current", "target": "edit-test", "overwrite": True})
cp = "edit-test"
# new DMR channel into a new zone (as the drawer does)
z = t("codeplug_upsert", {"codeplug": cp, "section": "zones", "entry": {"name": "Editor test", "A": [], "B": []}})
ch = t("codeplug_upsert", {"codeplug": cp, "section": "channels", "kind": "dmr", "entry": {"name": "SV1I TS2 test", "rxFrequency": 439.325, "txFrequency": 431.725, "rxOnly": False, "power": "High", "scanList": None, "colorCode": 1, "timeSlot": "TS2", "contact": "cont1", "groupList": None, "admit": "ColorCode"}})
t("codeplug_zone_set", {"codeplug": cp, "zone": z["id"], "channels": [ch["id"]], "mode": "append"})
# new FM channel with CTCSS
fm = t("codeplug_upsert", {"codeplug": cp, "section": "channels", "kind": "fm", "entry": {"name": "SR1A FM test", "rxFrequency": 145.65, "txFrequency": 145.05, "rxOnly": False, "power": None, "scanList": None, "bandwidth": "Wide", "admit": "Always", "txTone": {"ctcss": "88.5 Hz"}, "rxTone": None}})
t("codeplug_zone_set", {"codeplug": cp, "zone": z["id"], "channels": [fm["id"]], "mode": "append"})
# edit existing channel power + contact (merge)
t("codeplug_upsert", {"codeplug": cp, "section": "channels", "kind": "dmr", "entry": {"id": "ch926", "name": "Paneladiko", "rxFrequency": 438.4, "txFrequency": 430.8, "rxOnly": False, "power": "High", "scanList": None, "colorCode": 1, "timeSlot": "TS2", "contact": "cont1", "groupList": None, "admit": "Always"}})
# new contact + group list + scan list
c = t("codeplug_upsert", {"codeplug": cp, "section": "contacts", "kind": "dmr", "entry": {"name": "TG 2021 test", "number": 2021, "type": "GroupCall", "ring": False}})
g = t("codeplug_upsert", {"codeplug": cp, "section": "groupLists", "entry": {"name": "GL test", "contacts": [c["id"], "cont1"]}})
s = t("codeplug_upsert", {"codeplug": cp, "section": "scanLists", "entry": {"name": "Scan test", "channels": [ch["id"], fm["id"]]}})
v = t("codeplug_validate", {"codeplug": cp})
print("validate after edits:", v["ok"], v["messages"][:200])
full = t("codeplug_get", {"codeplug": cp, "section": "channels", "ids": [ch["id"], fm["id"], "ch926"], "full": True})["entries"]
for e in full: print(" ", json.dumps({k: e.get(k) for k in ("name","kind","rxFrequency","txFrequency","power","timeSlot","contact","txTone","admit")}, ensure_ascii=False))
# reorder + remove + delete
zz = t("codeplug_get", {"codeplug": cp, "section": "zones", "ids": [z["id"]]})["entries"][0]
t("codeplug_zone_set", {"codeplug": cp, "zone": z["id"], "channels": list(reversed(zz["A"])), "mode": "replace"})
d = t("codeplug_delete", {"codeplug": cp, "section": "channels", "ids": [fm["id"]]}); print("delete:", d)
d = t("codeplug_delete", {"codeplug": cp, "section": "contacts", "ids": [c["id"]]}); print("delete contact:", d)
v = t("codeplug_validate", {"codeplug": cp}); print("validate after deletes:", v["ok"])
print("diff lines vs current:", len(t("codeplug_diff", {"a": "current", "b": cp})["diff"].splitlines()))
