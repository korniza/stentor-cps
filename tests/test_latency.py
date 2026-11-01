# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
import httpx, time
H = {'X-Shack': '1'}; B = 'http://127.0.0.1:8000/ui/api/tool/'
def t(n, b):
    t0 = time.time(); r = httpx.post(B + n, json=b, headers=H, timeout=120); return round(time.time() - t0, 2), r.status_code
print('first load', t('codeplug_get', {'codeplug': 'edit-test', 'section': 'zones'}))
print('cached get', t('codeplug_get', {'codeplug': 'edit-test', 'section': 'zones'}))
for i in range(3):
    print('upsert', t('codeplug_upsert', {'codeplug': 'edit-test', 'section': 'contacts', 'kind': 'dmr', 'entry': {'name': 'T%d' % i, 'number': 9000 + i, 'type': 'GroupCall'}}))
print('zone_set', t('codeplug_zone_set', {'codeplug': 'edit-test', 'zone': 'zone81', 'channels': ['ch1'], 'mode': 'append'}))
print('channels list', t('codeplug_get', {'codeplug': 'edit-test', 'section': 'channels'}))
print('validate', t('codeplug_validate', {'codeplug': 'edit-test'}))
print('overview', t('codeplug_overview', {'codeplug': 'edit-test'}))
