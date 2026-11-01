# Changelog

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
