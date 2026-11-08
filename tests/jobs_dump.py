# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Print the last UI jobs, bridge state and the tail of the bridge trace (debug helper, run inside the container)."""
import httpx, json
H = {'X-Shack': '1'}; B = 'http://127.0.0.1:8000/ui/api/'
js = httpx.get(B + 'jobs', headers=H).json()
for j in js[-3:]:
    r = httpx.get(B + 'job/' + j['id'], headers=H).json()
    print(j['name'], j['state'], j.get('error'), round(r.get('elapsed', 0)), 's')
    print('   ', json.dumps(r.get('result'), ensure_ascii=False)[:1200])
print(httpx.get(B + 'bridge', headers=H).json())
for x in httpx.get(B + 'bridge/trace?last=12', headers=H).json():
    print(x['dir'], x['n'], x['hex'][:70])
