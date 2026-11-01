# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
import asyncio, json
from stentor import server as s

async def main():
    for name, coro in [("status", s.wpsd_status()), ("last_heard", s.wpsd_last_heard(8)),
                       ("dmrgateway", s.wpsd_config("dmrgateway")), ("bm", s.bm_device_info())]:
        try:
            print("==", name, json.dumps(await coro, default=str, ensure_ascii=False)[:1800])
        except Exception as e:
            print("==", name, "ERROR", type(e).__name__, e)

asyncio.run(main())
