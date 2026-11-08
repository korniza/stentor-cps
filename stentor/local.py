# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Where the radio code finds its settings and folders: one operator, everything from config (the env file)."""
from pathlib import Path

from . import config


def current_uid() -> None:
    return None


def current_user() -> None:
    return None


def workspace() -> Path:
    return config.WORKSPACE


def backup_dir() -> Path:
    return config.BACKUP_DIR


def qdmr_backup_dir() -> Path:
    return config.QDMR_BACKUP_DIR


def callsign() -> str:
    return config.CALLSIGN


def radio_model() -> str:
    return config.RADIO_MODEL


def radio_device() -> str:
    return config.RADIO_DEVICE
