# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Runtime configuration (all from environment)."""
import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


RADIO_MODEL = _env("RADIO_MODEL", "d878uv2")          # dmrconf --radio key
RADIO_DEVICE = _env("RADIO_DEVICE", "")               # e.g. /hostdev/ttyACM0 ; empty = auto-detect
DMR_ID = _env("DMR_ID", "")
CALLSIGN = _env("CALLSIGN", "N0CALL")

DATA_DIR = Path(_env("DATA_DIR", "/data"))
WORKSPACE = DATA_DIR / "workspace"                    # editable YAML codeplugs
BACKUP_DIR = Path(_env("BACKUP_DIR", "/backups"))     # backup tree (NAS mount)
QDMR_BACKUP_DIR = BACKUP_DIR / "qdmr"                 # radio reads made by this server
CPS_CODEPLUG_DIR = BACKUP_DIR / "codeplugs"           # .rdt files shipped by the Windows script

BM_API_KEY = _env("BM_API_KEY")                       # BrandMeister v2 API key (JWT) - optional
REPEATERBOOK_TOKEN = _env("REPEATERBOOK_TOKEN")       # optional
from . import __version__ as VERSION  # noqa: E402
HTTP_USER_AGENT = _env("HTTP_USER_AGENT", f"stentor/{VERSION} ({CALLSIGN})")

WPSD_HOST = _env("WPSD_HOST", "")                     # Pi-Star / WPSD hotspot (empty = no hotspot)
WPSD_PORT = int(_env("WPSD_PORT", "22"))
WPSD_USER = _env("WPSD_USER", "pi-star")
WPSD_PASSWORD = _env("WPSD_PASSWORD")
WPSD_KEY_FILE = _env("WPSD_KEY_FILE", str(DATA_DIR / "ssh" / "id_ed25519"))
# Hotspot backend: wpsd | pistar (both over ssh, same files) | openspot (HTTP API, experimental) | none.
# HOTSPOT_HOST/HOTSPOT_PASSWORD are the generic names; WPSD_* keep working for the ssh kinds.
HOTSPOT_KIND = _env("HOTSPOT_KIND", "wpsd" if WPSD_HOST else "none").lower()
HOTSPOT_HOST = _env("HOTSPOT_HOST", WPSD_HOST)
HOTSPOT_PASSWORD = _env("HOTSPOT_PASSWORD", WPSD_PASSWORD)
if HOTSPOT_KIND in ("wpsd", "pistar") and not WPSD_HOST:
    WPSD_HOST = HOTSPOT_HOST

READ_TIMEOUT = int(_env("RADIO_TIMEOUT", "600"))
WRITE_TIMEOUT = int(_env("RADIO_WRITE_TIMEOUT", "3600"))   # never kill dmrconf mid-write; the radio ACKs each block

for d in (WORKSPACE, DATA_DIR / "ssh"):
    d.mkdir(parents=True, exist_ok=True)
