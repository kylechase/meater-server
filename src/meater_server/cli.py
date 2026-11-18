"""Command line entry points: run the server, or inspect Bluetooth."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
import pathlib
import secrets
import signal
import socket
import subprocess
import sys

from . import __version__
from .ble import dump_gatt, scan_for_probes
from .config import Config
from .manager import MeaterManager
from .web import start_web

_LOGGER = logging.getLogger("meater_server")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meater-server",
        description="Read MEATER Bluetooth probes; serve a web UI and an ESPHome endpoint.",
    )
    parser.add_argument("--version", action="version", version=f"meater-server {__version__}")
    parser.add_argument("-c", "--config", help="path to a TOML config file")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="repeat for more")
    parser.add_argument("--adapter", help="Bluetooth adapter, e.g. hci0")

    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the server (the default)")
    run.add_argument("--web-port", type=int, help="override the web UI port")
    run.add_argument("--web-host", help="override the web UI bind address")
    run.add_argument("--esphome-port", type=int, help="override the ESPHome API port")
    run.add_argument("--no-web", action="store_true", help="do not serve the web UI")
    run.add_argument("--no-esphome", action="store_true", help="do not serve the ESPHome API")
    run.add_argument(
        "--units", choices=["C", "F"], help="units for the web UI (ESPHome is always C)"
    )

    scan = sub.add_parser("scan", help="scan for nearby probes")
    scan.add_argument("--timeout", type=float, default=10.0)
    scan.add_argument(
        "--all", action="store_true", help="list every BLE device, not only probes"
    )

    gatt = sub.add_parser("gatt", help="dump a probe's GATT table, for diagnostics")
    gatt.add_argument("address", help="probe MAC address")

    sub.add_parser("keygen", help="generate an ESPHome API encryption key")
    sub.add_parser("urls", help="print the addresses the web UI is reachable on")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "keygen":
        print(base64.b64encode(secrets.token_bytes(32)).decode())
        return 0

    try:
        config, source = Config.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"meater-server: {exc}", file=sys.stderr)
        return 2
    if source:
        _LOGGER.info("loaded config from %s", source)
    if args.adapter:
        config.adapter = args.adapter

    if args.command == "scan":
        return asyncio.run(_scan(config, args.timeout, args.all))
    if args.command == "gatt":
        return asyncio.run(_gatt(config, args.address))
    if args.command == "urls":
        _print_urls(config)
        return 0

    _apply_run_overrides(config, args)
    return asyncio.run(_run(config))


def _apply_run_overrides(config: Config, args: argparse.Namespace) -> None:
    for attr, value in (
        ("web_port", getattr(args, "web_port", None)),
        ("web_host", getattr(args, "web_host", None)),
        ("esphome_port", getattr(args, "esphome_port", None)),
        ("units", getattr(args, "units", None)),
    ):
        if value is not None:
            setattr(config, attr, value)
    if getattr(args, "no_web", False):
        config.web_enabled = False
    if getattr(args, "no_esphome", False):
        config.esphome_enabled = False


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    if os.environ.get("MEATER_LOG_LEVEL"):
        level = getattr(logging, os.environ["MEATER_LOG_LEVEL"].upper(), level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Default to INFO for our own loggers so a bare run still narrates itself.
    logging.getLogger("meater_server").setLevel(min(level, logging.INFO))
    logging.getLogger("bleak").setLevel(max(level, logging.WARNING))


def _primary_address() -> str | None:
    """The address this host uses to reach the wider network.

    This is the one a phone on the same wifi should be pointed at.  Opening a
    UDP socket to a reserved address makes the kernel pick the route without
    sending anything.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1; never routed
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return None


def _is_virtual(interface: str) -> bool:
    """Whether an interface is a container or VM bridge rather than a real link.

    Docker and libvirt bridges have addresses, but nothing a phone can reach,
    so listing them as ways to open the UI is just noise.
    """
    if interface.startswith(("docker", "veth", "virbr", "br-", "lo")):
        return True
    return (pathlib.Path("/sys/class/net") / interface / "bridge").is_dir()


def _lan_addresses() -> list[tuple[str, str]]:
    """Reachable ``(address, interface)`` pairs, most useful first."""
    found: list[tuple[str, str]] = []
    try:
        output = subprocess.run(
            ["ip", "-j", "-4", "addr"],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        ).stdout
        for entry in json.loads(output):
            name = entry.get("ifname", "")
            if _is_virtual(name):
                continue
            for address in entry.get("addr_info", []):
                value = address.get("local")
                if value and not value.startswith("127."):
                    found.append((value, name))
    except (OSError, ValueError, subprocess.SubprocessError):
        # No iproute2, or unexpected output: fall back to whatever the
        # resolver knows about this host.
        with contextlib.suppress(OSError):
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                value = info[4][0]
                if not value.startswith("127.") and all(v != value for v, _ in found):
                    found.append((value, ""))

    primary = _primary_address()
    found.sort(key=lambda pair: pair[0] != primary)
    return found


def _print_urls(config: Config) -> None:
    if not config.web_enabled:
        print("the web UI is disabled in config")
        return
    port = config.web_port
    print("Open the web UI at:\n")
    print(f"  this machine    http://localhost:{port}/")
    for address, interface in _lan_addresses():
        suffix = f"   ({interface})" if interface else ""
        print(f"  phone / laptop  http://{address}:{port}/{suffix}")
    print(f"  by name         http://{socket.gethostname()}.local:{port}/   (mDNS)")
    if config.web_host not in ("0.0.0.0", "::"):
        print(
            f"\nNote: the server binds {config.web_host}, so only that interface "
            "answers. Set web.host = \"0.0.0.0\" to reach it from a phone."
        )


async def _scan(config: Config, timeout: float, show_all: bool) -> int:
    print(f"scanning for {timeout:.0f}s on {config.adapter or 'the default adapter'}…")
    try:
        found = await scan_for_probes(timeout, config.adapter, all_devices=show_all)
    except Exception as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1
    if not found:
        print("\nno probes found.")
        print("A MEATER probe only advertises once it is out of its charging block.")
        print("Close the MEATER phone app too - a probe talks to one client at a time.")
        return 1
    print(f"\nfound {len(found)}:\n")
    for device, adv in found:
        name = adv.local_name or device.name or "(unnamed)"
        print(f"  {device.address}  {adv.rssi:>4} dBm  {name}")
        if adv.service_uuids:
            print(f"      services: {', '.join(adv.service_uuids)}")
        for company, payload in (adv.manufacturer_data or {}).items():
            print(f"      manufacturer 0x{company:04x}: {payload.hex(' ')}")
    print("\nPut an address in your config as:\n")
    print("  [bluetooth]")
    print(f'  addresses = ["{found[0][0].address}"]')
    return 0


async def _gatt(config: Config, address: str) -> int:
    try:
        print(await dump_gatt(address, config.adapter))
    except Exception as exc:
        print(f"could not inspect {address}: {exc}", file=sys.stderr)
        return 1
    return 0


async def _run(config: Config) -> int:
    manager = MeaterManager(config)
    runner = None

    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(getattr(signal, signal_name), manager.stop)

    try:
        if config.web_enabled:
            runner = await start_web(manager)
            _print_urls(config)
            print()
        await manager.run()
    finally:
        if runner is not None:
            await runner.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
