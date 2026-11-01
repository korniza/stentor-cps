# Stentor

*Στέντωρ — the herald of the Iliad, whose voice carried as far as fifty men's.*

Stentor is a self-hosted **web CPS (codeplug programming software) for DMR radios**. It wraps
[qdmr](https://github.com/hmatuschek/qdmr)'s `dmrconf` and adds the things the vendor CPS never will — all from your
browser:

- **Program the radio from any computer, in the browser.** Chrome/Edge open the radio's USB port with Web Serial and
  stream it over a WebSocket to the server, where a patched `dmrconf` talks to it through a pty. No drivers, no
  Windows, no USB on the server.
- **A verifying relay** in front of the radio (browser bridge or server USB) checks every frame and retries corrupt
  ones. (A differential write exists but is off: AnyTone radios erase the flash sector at END and program only the
  blocks they received, so skipping identical blocks wipes them — see CHANGELOG.)
- **A codeplug editor** (zones, channels, contacts, group lists, scan lists, FM broadcast memories) on qdmr's
  radio-independent YAML, with real validation (`dmrconf encode`), a diff against the last read, and
  confirm-before-write. Import an AnyTone CPS `.rdt`, export the CPS CSV table set.
- **Repeaters by region.** `import_region` pulls the repeaters of a country/region/city or radius
  (RadioID.net, and [RepeaterBook](https://www.repeaterbook.com) with your own API token — *data courtesy of
  RepeaterBook.com*) and builds channels per talkgroup, a zone and a roaming zone in one step. Ready-made FM
  presets (PMR446, LPD433, marine VHF, ISS, …) too.
- **FM radio.** Station lists per city (radiomap.eu), kept per codeplug and written to the radio's 100 FM memories —
  something no CPS offers.
- **Hotspot sync.** Reads a WPSD / Pi-Star hotspot over SSH (openSPOT over HTTP, experimental — see
  [docs/hotspots.md](docs/hotspots.md)) and reconciles its frequencies, colour code and talkgroups into the codeplug.
  Shows hotspot status, BrandMeister device state and last heard.
- **Contacts.** Downloads the RadioID user list, filters by country, writes the radio's Digital Contacts; a daily
  job keeps it fresh.
- **Backups.** Every read lands as YAML (optionally binary) in a dated tree on your NAS.

Status: **0.x — works for the author's AnyTone AT-D878UVII Plus + WPSD**, structured so other qdmr-supported radios
and Pi-Star-family hotspots are a configuration change.

## Architecture

```
browser (Chrome/Edge) ── https ─▶ reverse proxy ─▶ stentor container
   │  Web Serial                                     ├─ /ui      Shack web UI (static page + JSON API)
   └─ wss /ui/ws/serial ─────────────────────────────┼─ bridge   pty ⇄ WebSocket
                                                      ├─ dmrconf  (patched qdmr 0.15.1) read/write/encode
                                                      ├─ /data    workspace codeplugs (YAML), caches, ssh key
                                                      └─ /backups dated radio reads
hotspot (Pi-Star/WPSD) ◀── ssh ───────────────────────┘
```

## Quick start (single operator)

```bash
git clone https://github.com/<you>/stentor && cd stentor
cp deploy/example.env deploy/.env      # CALLSIGN, DMR_ID, RADIO_MODEL, hotspot, optional API keys
docker compose -f deploy/docker-compose.yml up -d --build
```

Put a TLS reverse proxy in front of port 8000 — the browser bridge requires `https`/`wss` — and restrict `/ui` to your
LAN or an SSO (the container only checks an `X-Shack: 1` header on the UI API).
Then open `https://<host>/ui/`, press **Ασύρματος εδώ / Radio here**, pick the radio's port, and **Read**.

Supported radios are whatever `dmrconf --list-radios` prints for qdmr 0.15 (AnyTone D868/D878/D878UVII/D578, BTECH
DMR-6X2, OpenGD77, Radioddity GD-77/GD-73, Baofeng DM-1701/RD-5R, TYT MD-UV380/390, Retevis RT3S, …). The browser
bridge works for radios that use **USB serial** (AnyTone/BTECH family, OpenGD77); DFU-based radios (TYT, Retevis,
Baofeng) need the server's USB for now.

## Repository layout

| Path | What |
|---|---|
| `stentor/` | Python package: `server.py` (the radio/codeplug/data functions the UI calls), `ui.py` (Starlette UI app), `bridge.py` (pty bridge), `radio.py` (dmrconf wrapper), `codeplug.py` (ruamel round-trip model + cache), `sync.py` (hotspot→codeplug), `wpsd.py`, `data.py` (RadioID/BrandMeister/RepeaterBook), `jobs.py`, `static/shack.html` (the UI) |
| `serve_http.py` | ASGI entry: serves the web UI |
| `Dockerfile`, `Dockerfile.dmrconf` | runtime image; patched dmrconf stage image |
| `patches/` | the qdmr patch (GPL-3.0, like qdmr) |
| `deploy/` | `docker-compose.yml` + `example.env` |
| `tests/` | API-level tests, run inside the container (`docker exec stentor python /tmp/test_editor.py`) |
| `VERSION`, `CHANGELOG.md` | semantic version, release notes |

## Versioning & releases

`VERSION` is the single source of truth: image tag, `server_info.version`, UI header. Release = bump `VERSION`,
add a `CHANGELOG.md` section, `git tag v<version>`, build. Pre-1.0: minor bumps may change function/route names.

## Security notes

- Device-changing actions (`radio_write`, `radio_write_callsign_db`, `wpsd_service`, `bm_static_talkgroup`)
  return a preview and a short-lived `confirm_token`; the change happens only on the second call with the token.
- `radio_write` reads a fresh backup first. Reads are never destructive.
- The bridge accepts one browser at a time and dies with the tab; the radio reboots after each session anyway.
- Secrets (`BM_API_KEY`, hotspot password) live only in the env file; `server_info` reports presence, never values.

## License

Copyright © 2026 Zacharias Kornilakis (SY1EKQ).

AGPL-3.0-or-later — see [LICENSE](LICENSE). This program is free software: you may redistribute and/or modify
it under the terms of the GNU Affero General Public License, version 3 or later, and it comes with ABSOLUTELY NO
WARRANTY. If you run a modified version as a network service, the AGPL requires you to offer its complete source
code to the users of that service.

Stentor bundles a modified `dmrconf` from qdmr (GPL-3.0); the modification is `patches/` and stays under
qdmr's licence. Release 0.1.0 was published under GPL-3.0-or-later; later releases are AGPL-3.0-or-later.

### Name

"Stentor" names this project. If you publish a fork or run a modified version for others, please give it a
different name so that users are not misled about where it comes from.
