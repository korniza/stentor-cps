# Changelog

## 0.3.2 - 2026-11-09

- Fixed (important): a channel's scan list never reached the radio — every channel was written with "no scan list"
  ("Scan list not selected" on AnyTone). qdmr 0.15 calls the channel's scan-list reference `scanListRef`; Stentor
  wrote `scanList`, which dmrconf ignores without a warning. Stentor keeps `scanList` and now gives dmrconf a
  temporary copy with qdmr's key on every write / verify / encode; `scanListRef` from a radio read is mapped back
  on load. Existing codeplugs need no change — just Write again. Test: `tests/test_scanlist_ref.py`.

## 0.3.1 - 2026-11-07

RepeaterBook, made steady (same code as the maintainer's instance, 0.4.91):

- One request per country per day (`REPEATERBOOK_TTL`, default 86400 s), cached on disk in
  `DATA_DIR/cache/repeaterbook/` (survives restarts); searches that arrive together wait for the same request.
- When RepeaterBook doesn't answer, the last copy is served and the next try waits 30 minutes; HTTP 429 is
  reported as a rate limit.
- Off-air repeaters are left out; "CSQ" and other non-numeric PL values are no longer taken as a CTCSS tone;
  narrow (12.5 kHz) FM repeaters get narrow channels; landmark and EchoLink/AllStar node numbers are kept.
- The repeater search note no longer says RepeaterBook is "coming".

## 0.3.0 - 2026-11-07

Radio communication brought up to date with the maintainer's own instance (Stentor 0.4.91):

- **Safer writes.** A write whose dmrconf timed out, or whose blocks were not acknowledged, is never committed: the
  relay refuses the radio's END, so the radio keeps its old content. Writes go out in batches of 16 blocks.
- **The write sends exactly what was previewed.** The codeplug is snapshotted before the safety-backup read (which
  replaces `last-read`); before, writing `last-read` sent the radio's old content back to it. After a write,
  `last-read` (YAML + FM memories) is what was written.
- **Much faster Read/Write over the browser bridge.** The page executes reads in batches (64–256 blocks, adaptive)
  and writes in batches of 64 with ACK-ahead, instead of one WebSocket round trip per 16-byte block; the relay
  pipelines reads on the server's own USB too.
- Progress bar with %, blocks and time left; the write shows its steps (backup → restart → write); per-phase times.
- D578UV: RX group lists no longer vanish after a write (lists placed in the slots the radio keeps enabled; a
  warning in the preview when they don't fit). Read timeout per model (D578UV up to 40 min).
- Radio GPS switched on (`--auto-enable-gps`) when channels send APRS — an init write used to leave it off.
- Digital Contacts write passes the DMR ID as text (failed with a numeric ID).
- dmrconf **p5**: AnyTone prefab SMS ("Quick Text") encoded as the CPS writes them (`patches/qdmr-0.15.1-sms-index.patch`).
- Wrong radio on the cable (not the `RADIO_MODEL` family): the read is kept as a dated backup but not as
  `last-read`, and nothing is written.
- Clear failure reasons (timeout, USB lost at n %, not in programming mode, port busy …); when the browser loses the
  radio's port, a snapshot of the last frames is kept in `DATA_DIR/traces/`. `DMRCONF_TRACE=1` keeps a verbose
  dmrconf trace of every run.
- AnyTone ports with the Artery AT32 USB chip (0x2E3C, newer D578UV) are offered in the port chooser; DTR/RTS are
  asserted for other CDC radios; a second tab takes the bridge over cleanly.
- **Experimental: TyT-family radios through the browser** (WebUSB DFU, dmrconf p3 `dfu-socket` patch) — tested with
  a simulator only, see the README.
- `dmrconf` runs resource-limited (`SANDBOX=1`, default).
- Rebuild the dmrconf stage image: `docker build -f Dockerfile.dmrconf -t stentor-dmrconf:0.15.1-p5 .`

## 0.2.1 - 2026-11-07

- RepeaterBook: the token is now sent in RepeaterBook's canonical `X-RB-App-Token` header (was `Authorization:
  Bearer`), and a new `REPEATERBOOK_USER_AGENT` setting is sent exactly as given, only to RepeaterBook —
  RepeaterBook matches an approved app's User-Agent exactly, so the default `stentor/<version> (<callsign>)` would be
  rejected. Without a token no request is made. README: how to get your own token.

## 0.2.0 - 2026-11-01

- Licence: **AGPL-3.0-or-later** from this release on (0.1.0 remains GPL-3.0-or-later). If you run a modified
  Stentor as a network service, you must offer its complete source code to its users. The qdmr patch in
  `patches/` keeps qdmr's GPL-3.0.
- Copyright and SPDX headers on every source file.
- README: licence section rewritten; forks and modified hosted versions are asked to use a different name.
- Removed an outdated roadmap document.

## 0.1.0

First public release of Stentor — a standalone, self-hosted **web CPS for DMR radios** (GPL-3.0-or-later).

- Browser programming over Web Serial → WebSocket → a patched `dmrconf` on the server (pty bridge): no drivers,
  no vendor CPS, no USB on the server.
- Verifying relay in front of the radio (browser bridge or server USB) that checks every frame and retries corrupt
  ones. Differential write is implemented but **off by default** — AnyTone radios erase the flash sector at END and
  program only the blocks they receive, so skipping identical blocks would wipe them.
- Codeplug editor on qdmr's radio-independent YAML — zones, channels, contacts, group lists, scan lists, FM
  broadcast memories — with `dmrconf encode` validation, a diff against the last read, and confirm-before-write.
  Import an AnyTone CPS `.rdt`; export the CPS CSV set.
- Repeaters by country / region / city / radius (RadioID.net; RepeaterBook with your own API token) → channels per
  talkgroup, a zone and a roaming zone in one step. Ready-made FM presets (PMR446, LPD433, marine VHF, ISS, SSTV).
- FM broadcast memories written to the radio's 100 FM slots, with per-city station lists (radiomap.eu).
- Hotspot sync for WPSD / Pi-Star over SSH (openSPOT over HTTP, experimental): reconciles frequencies, colour code
  and talkgroups into the codeplug; shows status, BrandMeister device state and last heard.
- RadioID Digital Contacts by country, with a daily refresh job.
- Dated YAML (optionally binary) backups of every read.

Derived from a private single-operator project the author ran for an AnyTone AT-D878UVII Plus and a WPSD hotspot.
