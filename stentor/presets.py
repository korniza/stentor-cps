# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Zacharias Kornilakis (SY1EKQ)
"""Ready-made FM channel sets ("special categories") that are internationally fixed: license-free PMR446/LPD433,
US FRS/GMRS and NOAA weather, the ISS, SSTV calling and the ITU Appendix 18 marine VHF channels.

Each preset is a function returning a list of channel dicts:
  {name, rx, tx, bw: "n"|"w", tone: <CTCSS Hz float|None>, rx_only: bool}
rx/tx are MHz. Marine and weather default to receive-only (monitoring): transmitting there needs the right
licence/equipment, which is the operator's call, so those are the conservative default.
"""


def _simplex(prefix: str, base: float, step: float, n: int, bw: str = "n", rx_only: bool = False,
             start: int = 1, digits: int = 2) -> list[dict]:
    out = []
    for i in range(n):
        out.append({"name": f"{prefix}{str(start + i).zfill(digits)}", "rx": round(base + i * step, 5),
                    "tx": round(base + i * step, 5), "bw": bw, "tone": None, "rx_only": rx_only})
    return out


def pmr446() -> list[dict]:
    """PMR446 - 16 analog FM channels, 12.5 kHz, license-free in Europe."""
    return _simplex("PMR", 446.00625, 0.0125, 16)


def lpd433() -> list[dict]:
    """LPD433 - 69 analog FM channels, 25 kHz, license-free in Europe (433.075-434.775 MHz)."""
    return _simplex("LPD", 433.075, 0.025, 69)


def gmrs_gr() -> list[dict]:
    """GMRS National Club of Greece (EETT-licensed): 4 UHF repeaters (CTCSS 203.5, -10 MHz) + 5 simplex channels.
    Source: gmrs.gr/frequencies.php. Membership required to use; carry the club licence card."""
    rpt = [("GMRS RP1", 460.8125, 450.8125), ("GMRS RP2", 460.8000, 450.8000),
           ("GMRS RP3", 460.8250, 450.8250), ("GMRS RP4", 460.8375, 450.8375)]
    out = [{"name": n, "rx": rx, "tx": tx, "bw": "n", "tone": 203.5, "rx_only": False} for n, rx, tx in rpt]
    sx = [443.88750, 443.98750, 444.26250, 444.51250, 444.97500]
    out += [{"name": f"GMRS {str(i + 1).zfill(2)}", "rx": f, "tx": f, "bw": "n", "tone": None, "rx_only": False}
            for i, f in enumerate(sx)]
    return out


def iss() -> list[dict]:
    """International Space Station: voice/SSTV downlink, APRS digipeater, and the 437.800/145.990 cross-band repeater."""
    return [
        {"name": "ISS Voice", "rx": 145.800, "tx": 145.800, "bw": "w", "tone": None, "rx_only": True},
        {"name": "ISS SSTV", "rx": 145.800, "tx": 145.800, "bw": "w", "tone": None, "rx_only": True},
        {"name": "ISS APRS", "rx": 145.825, "tx": 145.825, "bw": "n", "tone": None, "rx_only": False},
        {"name": "ISS Rptr", "rx": 437.800, "tx": 145.990, "bw": "w", "tone": 67.0, "rx_only": False},
    ]


def sstv() -> list[dict]:
    """SSTV calling frequencies (Region 1 2 m; the HF ones need an HF radio)."""
    return [{"name": "SSTV 2m", "rx": 144.500, "tx": 144.500, "bw": "w", "tone": None, "rx_only": False}]


# ITU Appendix 18 international VHF marine channels: ship-transmit / ship-receive (MHz). Equal = simplex.
_MARINE = [
    ("01", 156.050, 160.650), ("02", 156.100, 160.700), ("03", 156.150, 160.750), ("04", 156.200, 160.800),
    ("05", 156.250, 160.850), ("06", 156.300, 156.300), ("07", 156.350, 160.950), ("08", 156.400, 156.400),
    ("09", 156.450, 156.450), ("10", 156.500, 156.500), ("11", 156.550, 156.550), ("12", 156.600, 156.600),
    ("13", 156.650, 156.650), ("14", 156.700, 156.700), ("15", 156.750, 156.750), ("16", 156.800, 156.800),
    ("17", 156.850, 156.850), ("18", 156.900, 161.500), ("19", 156.950, 161.550), ("20", 157.000, 161.600),
    ("21", 157.050, 161.650), ("22", 157.100, 161.700), ("23", 157.150, 161.750), ("24", 157.200, 161.800),
    ("25", 157.250, 161.850), ("26", 157.300, 161.900), ("27", 157.350, 161.950), ("28", 157.400, 162.000),
    ("60", 156.025, 160.625), ("61", 156.075, 160.675), ("62", 156.125, 160.725), ("63", 156.175, 160.775),
    ("64", 156.225, 160.825), ("65", 156.275, 160.875), ("66", 156.325, 160.925), ("67", 156.375, 156.375),
    ("68", 156.425, 156.425), ("69", 156.475, 156.475), ("70", 156.525, 156.525), ("71", 156.575, 156.575),
    ("72", 156.625, 156.625), ("73", 156.675, 156.675), ("74", 156.725, 156.725), ("75", 156.775, 156.775),
    ("76", 156.825, 156.825), ("77", 156.875, 156.875), ("78", 156.925, 161.525), ("79", 156.975, 161.575),
    ("80", 157.025, 161.625), ("81", 157.075, 161.675), ("82", 157.125, 161.725), ("83", 157.175, 161.775),
    ("84", 157.225, 161.825), ("85", 157.275, 161.875), ("86", 157.325, 161.925), ("87", 157.375, 157.375),
    ("88", 157.425, 157.425),
]


def marine() -> list[dict]:
    """ITU Appendix 18 international VHF marine channels (receive only - listen on the coast-station frequency)."""
    out = []
    for ch, ship_tx, ship_rx in _MARINE:
        out.append({"name": f"MAR{ch}", "rx": ship_rx, "tx": ship_tx, "bw": "n", "tone": None, "rx_only": True})
    return out


# id -> (label, region, builder). Order is the display order.
PRESETS = {
    "pmr446": ("PMR446", "EU", pmr446),
    "lpd433": ("LPD433", "EU", lpd433),
    "gmrs_gr": ("GMRS Greece", "GR", gmrs_gr),
    "marine": ("Marine VHF", "ITU", marine),
    "iss": ("ISS", "—", iss),
    "sstv": ("SSTV", "—", sstv),
}


def catalog() -> list[dict]:
    return [{"id": k, "label": lbl, "region": reg, "count": len(fn())} for k, (lbl, reg, fn) in PRESETS.items()]


def build(preset_id: str) -> list[dict]:
    if preset_id not in PRESETS:
        raise ValueError(f"unknown preset '{preset_id}'. Valid: {', '.join(PRESETS)}")
    return PRESETS[preset_id][2]()


def default_zone(preset_id: str) -> str:
    return PRESETS[preset_id][0][:16]
