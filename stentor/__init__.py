# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Stentor - self-hosted web CPS for DMR radios (qdmr-based)."""
from pathlib import Path

def _read_version() -> str:
    for p in (Path(__file__).resolve().parent.parent / "VERSION", Path("/app/VERSION")):
        if p.exists():
            return p.read_text().strip()
    return "0.0.0-dev"

__version__ = _read_version()
