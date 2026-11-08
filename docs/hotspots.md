# Hotspot backends

Stentor talks to one hotspot, selected with `HOTSPOT_KIND`:

| kind | transport | status | notes |
|---|---|---|---|
| `wpsd` | ssh (`HOTSPOT_HOST`, `WPSD_USER`, key or `HOTSPOT_PASSWORD`) | tested daily | W0CHP's WPSD; `/etc/mmdvmhost`, `/etc/dmrgateway`, `/etc/WPSD-release` |
| `pistar` | ssh, same as above | expected to work | classic Pi-Star 4.x: same files and services, release file `/etc/pistar-release` |
| `openspot` | HTTP API of the device's web UI (`HOTSPOT_HOST`, `HOTSPOT_PASSWORD`) | **experimental, untested** | SharkRF openSPOT 2/3/4; status, frequencies, last heard, reboot. No shell, no config files - the active connector is presented as a one-network DMRGateway with pass-all TGs so the codeplug sync works |
| `none` | - | - | no hotspot panels / sync |

All of them answer the same calls (`wpsd_status`, `wpsd_last_heard`, `wpsd_config`, `wpsd_service`, the sync in
`codeplug_sync_wpsd`); the tool names kept their historical `wpsd_` prefix.

## Adding a backend

`stentor/hotspot.py` dispatches to a module that implements `status()`, `last_heard(limit, include_rf_only)`,
`read_config(which, section)`, `service_action(action)` and `run(cmd, timeout)` (raise for backends without a
shell). `read_config("mmdvmhost")` must return `General/Info/DMR` sections and `read_config("dmrgateway")`
`DMR Network N` sections in MMDVMHost/DMRGateway vocabulary - that is what the sync logic reads.

## What the WPSD/Pi-Star backend reads and writes

Read: `/etc/mmdvmhost`, `/etc/dmrgateway` (passwords masked), `/var/log/pi-star/MMDVM-*.log` (last heard),
`systemctl is-active` for the gateway services. Write: service restarts / reboot only (two-step with
confirmation). Configuration changes stay in the hotspot's own dashboard.

## Lessons from the MMDVM_HS_Dual_Hat (2026-10)

* AnyTone VFO transmits colour code 0: always test a hotspot from a codeplug channel.
* "Repeater not found" on the AnyTone means the hotspot never answered the wake-up - frequencies, colour code,
  RX offset or a dead receiver; a simplex hotspot (RX = TX) with admit `Always` avoids the handshake altogether.
* `MMDVMCal` needs the baud rate and MMDVMHost stopped: `sudo wpsd-services fullstop; sudo MMDVMCal 115200 /dev/ttyAMA0`.
  Its BER test runs the modem in simplex (chip 1); its RSSI mode reads chip 2 (the duplex receiver) - a flat RSSI
  with a handheld keyed next to the board means that chip is dead, not mis-tuned.
