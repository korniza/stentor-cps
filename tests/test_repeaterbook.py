# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""RepeaterBook client (stentor/data.py): no request without a token, the approved User-Agent and the X-RB-App-Token
header, one request per country per TTL shared by concurrent callers, disk cache that survives a restart, a stale copy
on errors with back-off, off-air rows dropped, tones/bandwidth/EchoLink parsed. httpx is mocked; no network.
Run: DATA_DIR=<empty> HOTSPOT_KIND=none python tests/test_repeaterbook.py"""
import asyncio
import sys
import time

import httpx

from stentor import config
from stentor import data

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  [{str(extra)[:400]}]" if extra and not cond else ""))


ROWS = [
    {"Rptr ID": 1, "Callsign": "SV1A", "Frequency": "145.600000", "Input Freq": "145.00000", "PL": "88.5", "TSQ": "",
     "Nearest City": "Athens", "Landmark": "Ymittos", "State": "Greece", "Region": None, "FM Analog": "Yes", "DMR": "No",
     "Operational Status": "On-air", "Use": "OPEN", "FM Bandwidth": "12.5 kHz", "EchoLink Node": "123456",
     "AllStar Node": "0", "Lat": "37.9", "Long": "23.8"},
    {"Rptr ID": 2, "Callsign": "SV1B", "Frequency": "438.800000", "Input Freq": "431.20000", "PL": "CSQ",
     "Nearest City": "Penteli", "State": "Greece", "FM Analog": "No", "DMR": "Yes", "DMR Color Code": "1",
     "Operational Status": "On-air", "Lat": "38.0", "Long": "23.9"},
    {"Rptr ID": 3, "Callsign": "SV1C", "Frequency": "144.475000", "Input Freq": "144.47500", "PL": "88.5",
     "Nearest City": "Anogeia", "State": "Greece", "FM Analog": "Yes", "DMR": "No", "Operational Status": "Off-air"},
]
calls = []
fail = {"on": False}


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"{self.status_code}", request=None, response=None)

    def json(self):
        return self._p


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        calls.append({"url": url, "params": params, "headers": headers})
        await asyncio.sleep(0.05)
        if fail["on"]:
            return FakeResp(503, {})
        return FakeResp(200, {"count": len(ROWS), "results": ROWS})


data.httpx.AsyncClient = FakeClient
UA = "Stentor/1.0 (+https://example.org; N0CALL; a@b.c)"


async def main():
    # 1. no token -> no request
    config.REPEATERBOOK_TOKEN = ""
    try:
        await data.repeaterbook("Greece")
        check("no token raises", False)
    except RuntimeError as e:
        check("no token raises 401-like error", str(e).startswith("401"), e)
    check("no request without token", not calls, calls)

    # 2. token + UA, concurrent callers share one request
    config.REPEATERBOOK_TOKEN = "app_test"
    config.REPEATERBOOK_USER_AGENT = UA
    res = await asyncio.gather(*(data.repeaterbook("Greece") for _ in range(5)))
    check("5 concurrent callers -> 1 request", len(calls) == 1, calls)
    h = calls[0]["headers"]
    check("X-RB-App-Token header", h.get("X-RB-App-Token") == "app_test", h)
    check("approved User-Agent sent exactly", h.get("User-Agent") == UA, h)
    check("exportROW with country", calls[0]["url"].endswith("/api/exportROW.php") and calls[0]["params"] == {"country": "Greece"})
    r = res[0]
    check("off-air dropped", [x["callsign"] for x in r] == ["SV1A", "SV1B"], r)
    a, b = r
    check("FM fields", a["modes"] == ["FM"] and a["ctcss"] == "88.5" and a["bandwidth"] == "Narrow"
          and a["echolink"] == "123456" and a["allstar"] is None and a["landmark"] == "Ymittos", a)
    check("offset", a["offset_mhz"] == -0.6, a["offset_mhz"])
    check("CSQ is not a tone", b["ctcss"] is None and b["modes"] == ["DMR"], b)
    check("attribution", data.attribution_for(r) == data.RB_ATTRIBUTION)

    # 3. disk cache survives a restart (memory cleared)
    data._rb_mem.clear()
    await data.repeaterbook("greece")
    check("disk cache: no new request after restart", len(calls) == 1, len(calls))
    st = data.repeaterbook_status()
    check("status lists the cached country", st["cached"].get("greece", {}).get("count") == 3 and st["user_agent"] == UA, st)

    # 4. expired + RB error -> stale copy, then back-off (no hammering)
    data._rb_mem["greece"] = (time.time() - config.REPEATERBOOK_TTL - 10, data._rb_mem["greece"][1])
    data._rb_cache_file("greece").unlink()
    fail["on"] = True
    r2 = await data.repeaterbook("Greece")
    check("error -> stale copy served", len(r2) == 2 and len(calls) == 2, len(calls))
    await data.repeaterbook("Greece")
    check("back-off: no retry right after a failure", len(calls) == 2, len(calls))
    data._rb_fail["greece"] = 0
    fail["on"] = False
    await data.repeaterbook("Greece")
    check("retry after back-off refreshes the cache", len(calls) == 3 and time.time() - data._rb_mem["greece"][0] < 5)

    # 5. error without any cache -> raises
    fail["on"] = True
    try:
        await data.repeaterbook("Cyprus")
        check("no cache + error raises", False)
    except Exception:  # noqa: BLE001
        check("no cache + error raises", True)

    # 6. UA falls back to the general one when unset
    config.REPEATERBOOK_USER_AGENT = ""
    fail["on"] = False
    data._rb_mem.clear()
    data._rb_fail.clear()
    data._rb_cache_file("greece").unlink(missing_ok=True)
    await data.repeaterbook("Greece")
    check("UA fallback = HTTP_USER_AGENT", calls[-1]["headers"].get("User-Agent") == config.HTTP_USER_AGENT, calls[-1]["headers"])
    check("no Bearer header", "Authorization" not in calls[-1]["headers"], calls[-1]["headers"])


asyncio.run(main())
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
