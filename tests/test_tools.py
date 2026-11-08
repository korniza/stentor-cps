# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
import asyncio, json, shutil, os
from stentor import server as s, codeplug as cp, config

def show(t, v, n=700):
    print(f"== {t}: {json.dumps(v, default=str)[:n]}")

async def main():
    shutil.copy("/test/potsdam.yaml", config.WORKSPACE / "orig.yaml")
    shutil.copy("/test/potsdam.yaml", config.WORKSPACE / "current.yaml")
    show("validate orig", await s.codeplug_validate("orig"))
    s.codeplug_add_talkgroup("current", 202, "Greece", ["GR"])
    s.codeplug_add_channel("current", "SZ1ATH TS1", "dmr", 438.6, offset_mhz=-7.6, color_code=1, time_slot=1, contact="202", group_list="GR", zones=["Athens"])
    s.codeplug_add_channel("current", "SR1A FM", "fm", 145.65, offset_mhz=-0.6, ctcss_tx=88.5, zones=["Athens"])
    s.codeplug_settings("current", "settings.introLine1", "N0CALL", True)
    s.codeplug_delete("current", "contacts", ["cont4"])
    show("validate edited", await s.codeplug_validate("current"))
    show("dmr id", await s.dmr_id_lookup("N0CALL"))
    show("tg 202x", await s.talkgroups_search(prefix="202", limit=15))
    rp = await s.repeaters_search("Greece", mode="DMR", limit=5)
    show("repeaters DMR", rp, 1500)
    rp2 = await s.repeaters_search("Greece", mode="FM", near_lat=37.98, near_lon=23.73, radius_km=40, source="repeaterbook", limit=5)
    show("repeaters FM near Athens", rp2, 1500)
    if rp["repeaters"]:
        show("import", await s.codeplug_import_repeaters("current", rp["repeaters"][:2], "GR DMR"))
        show("validate after import", await s.codeplug_validate("current"))
    show("diff", s.codeplug_diff("orig", "current")["diff"], 2500)
    show("write preview (no radio)", await s.radio_write("current"), 600)
    show("detect", await s.radio_detect())
    show("pubkey", s.wpsd_ssh_pubkey())
    try:
        show("wpsd", await s.wpsd_status())
    except Exception as e:
        print("wpsd error:", type(e).__name__, e)
    try:
        show("bm device", await s.bm_device_info("2021"), 600)
    except Exception as e:
        print("bm error:", e)
    show("info", s.server_info())

asyncio.run(main())
