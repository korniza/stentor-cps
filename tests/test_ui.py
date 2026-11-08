# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
import httpx, time
B = "http://127.0.0.1:8000"
H = {"X-Shack": "1"}
for tool, body in [("wpsd_status", {}), ("codeplug_list", {"include_backups": False}),
                   ("codeplug_get", {"codeplug": "current", "section": "zones", "limit": 4000}),
                   ("wpsd_sync_status", {"codeplug": "current"})]:
    t = time.time()
    r = httpx.post(f"{B}/ui/api/tool/{tool}", json=body, headers=H, timeout=60)
    print(tool, r.status_code, f"{time.time()-t:.1f}s", r.text[:150].replace("\n", " "))
print("blocked tool:", httpx.post(f"{B}/ui/api/tool/radio_write", json={}, headers=H).status_code)
print("no header:", httpx.post(f"{B}/ui/api/tool/wpsd_status", json={}).status_code)
