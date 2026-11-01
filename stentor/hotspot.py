# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Hotspot backend dispatcher: the server and the sync logic talk to this module, which forwards to the backend
selected by HOTSPOT_KIND - WPSD and Pi-Star over ssh (stentor.wpsd: same files, same services, only the release
file differs), openSPOT over HTTP (stentor.openspot, experimental), or none."""
from . import config

KINDS = ("wpsd", "pistar", "openspot", "none")


def kind() -> str:
    return config.HOTSPOT_KIND if config.HOTSPOT_KIND in KINDS else "none"


def _backend():
    k = kind()
    if k in ("wpsd", "pistar"):
        from . import wpsd
        return wpsd
    if k == "openspot":
        from . import openspot
        return openspot
    raise RuntimeError("No hotspot configured (HOTSPOT_KIND=none). Set HOTSPOT_KIND=wpsd|pistar|openspot and HOTSPOT_HOST.")


def configured() -> bool:
    return kind() != "none" and bool(config.HOTSPOT_HOST)


async def status() -> dict:
    st = await _backend().status()
    st.setdefault("kind", kind())
    return st


async def last_heard(limit: int = 25, include_rf_only: bool = False) -> list[dict]:
    return await _backend().last_heard(limit, include_rf_only)


async def read_config(which: str, section: str = "") -> dict:
    return await _backend().read_config(which, section)


async def service_action(action: str) -> dict:
    return await _backend().service_action(action)


async def run(cmd: str, timeout: int = 30) -> tuple[int, str]:
    return await _backend().run(cmd, timeout)


def ssh_pubkey() -> str:
    from . import wpsd
    return wpsd.ensure_keypair()
