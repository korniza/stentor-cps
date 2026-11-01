# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
import asyncio, json
from stentor import server as s

async def main():
    d = await s.dashboard_snapshot()
    print("== dash keys", list(d))
    for k in ("hotspot", "brandmeister", "last_heard", "codeplug", "sync"):
        v = d.get(k)
        print("==", k, json.dumps(v, default=str, ensure_ascii=False)[:900])
    r = await s.codeplug_sync_wpsd("wpsd-draft", dry_run=True)
    print("== sync dry", json.dumps({k: r[k] for k in ("hotspot", "networks", "desired_channels", "before")}, ensure_ascii=False)[:2500])

asyncio.run(main())
