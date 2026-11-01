# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Unattended jobs, run from the host:  docker exec stentor python -m stentor.jobs update-contacts [--force]

update-contacts: refresh the radio's Digital Contacts with the Greek RadioID users, but only when the list has
changed since the last successful write (or --force) and the radio is connected. Exit codes:
0 = written or already current, 2 = radio not connected (try again later), 1 = error.
"""
import asyncio
import hashlib
import json
import sys
from datetime import datetime

from . import config, data, radio

STATE = config.DATA_DIR / "contacts-state.json"
COUNTRIES = ["Greece"]


def _fingerprint(path: str) -> str:
    users = json.load(open(path))["users"]
    rows = sorted((u.get("radio_id"), u.get("callsign"), u.get("fname"), u.get("surname"), u.get("city")) for u in users)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


async def update_contacts(force: bool) -> tuple[int, dict]:
    path, count, per = await data.radioid_userdb(COUNTRIES, max_age_h=12)
    fp = _fingerprint(path)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    res = {"time": datetime.now().isoformat(timespec="seconds"), "countries": COUNTRIES, "entries": count,
           "previous_entries": state.get("entries"), "last_written": state.get("written_at")}
    if not force and state.get("fingerprint") == fp:
        res["result"] = "unchanged"
        return 0, res
    det = await radio.detect()
    if not det["ok"]:
        res["result"] = "radio_not_connected"
        return 2, res
    w = await radio.write_callsign_db(count, config.DMR_ID, path)
    if not w["ok"]:
        res["result"] = "write_failed"
        res["output"] = w["output"][-800:]
        return 1, res
    STATE.write_text(json.dumps({"fingerprint": fp, "entries": count, "written_at": res["time"]}))
    res["result"] = "written"
    return 0, res


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "update-contacts":
        print(__doc__)
        sys.exit(1)
    try:
        code, res = asyncio.run(update_contacts("--force" in sys.argv))
    except Exception as e:  # noqa: BLE001
        code, res = 1, {"result": "error", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(res, ensure_ascii=False))
    sys.exit(code)


if __name__ == "__main__":
    main()
