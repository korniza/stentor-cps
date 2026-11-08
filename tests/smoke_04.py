# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Smoke test for 0.4 features against a running server (inside the container: python /tmp/smoke_04.py)."""
import httpx

H = {"X-Shack": "1"}
B = "http://127.0.0.1:8000/ui/api/"
print("version", httpx.post(B + "tool/server_info", headers=H, json={}).json().get("version"))
print("bridge", httpx.get(B + "bridge", headers=H).json())
r = httpx.post(B + "tool/fm_cities", headers=H, json={"country": "gr"}, timeout=40).json()
print("cities", r.get("cities", r)[:3])
r = httpx.post(B + "tool/fm_stations", headers=H, json={"country": "gr", "city": "athina"}, timeout=40).json()
print("stations", r.get("count"), (r.get("stations") or [None])[0])
r = httpx.get(B + "export/cps?codeplug=current&shack=1", headers=H)
print("export", r.status_code, r.headers.get("content-disposition"), len(r.content))
r = httpx.post(B + "tool/codeplug_import_region", headers=H, timeout=60,
               json={"codeplug": "current", "zone": "Attiki DMR", "country": "Greece", "region": "Attik", "band": "70cm",
                     "dry_run": True, "talkgroups": [{"tg": 202, "ts": 2}]}).json()
print("region", {k: v for k, v in r.items() if k != "repeaters"})
print("region sample", r.get("repeaters", [])[:3])
r = httpx.post(B + "tool/fm_list", headers=H, json={"codeplug": "current"}).json()
print("fm_list", r)
