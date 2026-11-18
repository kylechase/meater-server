"""Bluetooth transport: find MEATER probes, stay connected, decode readings.

One task per slot owns one probe's connection and retries forever.  A separate
discovery loop runs only while a slot is still empty, because BlueZ handles
scanning and connecting at the same time poorly.

The probe pushes notifications on both characteristics it exposes.  Some
firmware builds leave the battery characteristic read-only, so the manager
falls back to polling whatever it could not subscribe to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from bleak import AdvertisementData, BleakClient, BleakScanner, BLEDevice
from bleak.exc import BleakError

from .config import Config
from .decode import (
    BATTERY_CHAR_UUID,
    TEMPERATURE_CHAR_UUID,
    DecodeError,
    decode_battery,
    decode_temperatures,
    is_plausible_celsius,
    looks_like_meater,
)
from .state import Reading

_LOGGER = logging.getLogger(__name__)

ReadingCallback = Callable[[int, str, Reading], Awaitable[None] | None]
ConnectionCallback = Callable[[int, str, bool, str | None], Awaitable[None] | None]
AssignCallback = Callable[[int, str, str | None], Awaitable[None] | None]

#: How often to poll a characteristic that would not notify.
_POLL_INTERVAL_S = 5.0
#: Longest backoff between reconnection attempts.
_MAX_BACKOFF_S = 60.0
#: Scans a remembered probe may miss before its slot is offered to another.
_RELEASE_AFTER_SCANS = 3
#: BlueZ reports this when it has no signal reading, rather than omitting it.
_RSSI_UNAVAILABLE = -127


def _manufacturer_ids(adv: AdvertisementData | None) -> list[int]:
    if adv is None or not adv.manufacturer_data:
        return []
    return list(adv.manufacturer_data.keys())


async def scan_for_probes(
    timeout: float = 10.0, adapter: str | None = None, all_devices: bool = False
) -> list[tuple[BLEDevice, AdvertisementData]]:
    """Scan and return MEATER probes, strongest signal first.

    With ``all_devices`` set, return everything seen - useful when a probe is
    not matching the usual advertisement and you need to see what is there.
    """
    kwargs: dict = {"adapter": adapter} if adapter else {}
    found = await BleakScanner.discover(timeout=timeout, return_adv=True, **kwargs)
    results = [
        (device, adv)
        for device, adv in found.values()
        if all_devices
        or looks_like_meater(
            adv.local_name or device.name, list(adv.service_uuids), _manufacturer_ids(adv)
        )
    ]
    results.sort(key=lambda pair: pair[1].rssi or -999, reverse=True)
    return results


async def known_probes_from_adapter(adapter: str | None = None) -> list[BLEDevice]:
    """Probes the adapter already knows about, advertising or not.

    A connected BLE device stops advertising, so a probe left connected by a
    crashed process is invisible to scanning and could never be recovered by
    discovery alone.  BlueZ still has it in its object tree, and will still
    connect to it, so this fills that gap.

    The returned devices carry their D-Bus object path, which is what lets
    bleak connect straight to them - handing it a bare address instead makes
    it scan, and scanning is exactly what does not work here.

    Best effort by design: it reads bleak's BlueZ backend state, so it returns
    nothing on other platforms or if that internal shape ever changes, and the
    caller simply falls back to ordinary scanning.
    """
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager

        manager = await get_global_bluez_manager()
        properties = manager._properties
    except Exception as exc:
        _LOGGER.debug("could not read known devices from the adapter: %s", exc)
        return []

    out: list[BLEDevice] = []
    for path, interfaces in properties.items():
        device = interfaces.get("org.bluez.Device1")
        if not device:
            continue
        if adapter and f"/{adapter}/" not in path:
            continue
        name = device.get("Name") or device.get("Alias")
        uuids = [str(u) for u in (device.get("UUIDs") or [])]
        address = device.get("Address")
        if address and looks_like_meater(name, uuids, []):
            out.append(
                BLEDevice(
                    str(address).upper(),
                    name,
                    {"path": path, "props": device},
                )
            )
    return out


async def dump_gatt(address: str, adapter: str | None = None, timeout: float = 25.0) -> str:
    """Connect to a probe and describe its GATT table, for diagnostics."""
    kwargs: dict = {"adapter": adapter} if adapter else {}
    lines: list[str] = []
    async with BleakClient(address, timeout=timeout, **kwargs) as client:
        lines.append(f"connected to {address}")
        for service in client.services:
            lines.append(f"\nservice {service.uuid}  ({service.description})")
            for char in service.characteristics:
                props = ",".join(char.properties)
                value = ""
                if "read" in char.properties:
                    try:
                        raw = await client.read_gatt_char(char)
                        value = f"  = {bytes(raw).hex(' ')}"
                        if char.uuid.lower() == TEMPERATURE_CHAR_UUID and len(raw) >= 6:
                            tip, ambient = decode_temperatures(bytes(raw))
                            value += f"  -> tip {tip:.1f} C, ambient {ambient:.1f} C"
                        elif char.uuid.lower() == BATTERY_CHAR_UUID and len(raw) >= 2:
                            value += f"  -> battery {decode_battery(bytes(raw))}%"
                    except (BleakError, DecodeError, OSError) as exc:
                        value = f"  (read failed: {exc})"
                lines.append(f"  char {char.uuid}  [{props}]{value}")
    return "\n".join(lines)


class BleManager:
    """Owns every probe connection and reports readings upward."""

    def __init__(
        self,
        config: Config,
        on_reading: ReadingCallback,
        on_connection: ConnectionCallback,
        on_assign: AssignCallback | None = None,
    ) -> None:
        self._config = config
        self._on_reading = on_reading
        self._on_connection = on_connection
        self._on_assign = on_assign
        self._assignments: dict[int, str] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._rssi: dict[str, int] = {}
        self._battery: dict[str, int] = {}
        self._names: dict[str, str] = {}
        self._devices: dict[str, BLEDevice] = {}
        # Slots whose address came from config: never reassigned, because the
        # user asked for that specific probe.
        self._pinned: set[int] = set()
        self._absent_scans: dict[int, int] = {}
        self._connected: set[int] = set()
        # BlueZ allows one operation at a time on an adapter: a second connect
        # while one is in flight, or a scan while a connect is running, both
        # fail with "Operation already in progress".  Scans and the connect
        # phase therefore take turns through this lock.  The notification
        # streams that follow a connect are not serialised - only the setup.
        self._adapter_lock = asyncio.Lock()
        self._discovery_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # -- assignment ----------------------------------------------------------

    def seed_assignments(
        self, assignments: dict[int, str], pinned: set[int] | None = None
    ) -> None:
        """Pre-bind slots to addresses, from config or the saved state file.

        ``pinned`` slots came from config and are held forever.  The rest are
        only remembered from last time, so a slot can be handed to a probe
        that is actually present if the remembered one stays away - a MEATER
        sleeps in its charging block and simply will not answer.
        """
        for index, address in assignments.items():
            if address:
                self._assignments[index] = address.upper()
        self._pinned = set(pinned or ())

    def assigned_addresses(self) -> dict[int, str]:
        return dict(self._assignments)

    def _free_slots(self) -> list[int]:
        return [i for i in range(1, self._config.slots + 1) if i not in self._assignments]

    def _pending_slots(self) -> list[int]:
        """Slots that still need a probe found: unbound, or bound but offline."""
        return sorted(
            set(self._free_slots())
            | {index for index in self._assignments if index not in self._connected}
        )

    def _needs_discovery(self) -> bool:
        """Whether scanning would help right now.

        Scanning competes with established connections, so it runs only while
        a slot is unbound or a bound probe is offline.  A disconnected probe
        has to be re-discovered before BlueZ will connect to it by address at
        all, so this is not just about finding new probes.
        """
        return bool(self._pending_slots())

    async def _assign(self, index: int, address: str, name: str | None) -> None:
        address = address.upper()
        self._assignments[index] = address
        if name:
            self._names[address] = name
        _LOGGER.info("slot %d bound to probe %s (%s)", index, address, name or "unnamed")
        await _maybe_await(self._on_assign, index, address, name)

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Run until :meth:`stop` is called."""
        self._discovery_task = asyncio.create_task(
            self._discovery_loop(), name="meater-discovery"
        )
        try:
            while not self._stopping.is_set():
                self._ensure_connection_tasks()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=2.0)
        finally:
            await self._shutdown()

    def stop(self) -> None:
        self._stopping.set()

    async def _shutdown(self) -> None:
        tasks = [*self._tasks.values()]
        if self._discovery_task is not None:
            tasks.append(self._discovery_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._discovery_task = None

    def _ensure_connection_tasks(self) -> None:
        for index, address in self._assignments.items():
            task = self._tasks.get(index)
            if task is None or task.done():
                self._tasks[index] = asyncio.create_task(
                    self._probe_loop(index, address), name=f"meater-probe-{index}"
                )

    # -- discovery -----------------------------------------------------------

    async def _discovery_loop(self) -> None:
        """Scan for probes while any slot still needs one."""
        while not self._stopping.is_set():
            if not self._needs_discovery():
                # Every probe is connected; stay off the air and re-check soon.
                await self._sleep_or_stop(15.0)
                continue
            try:
                async with self._adapter_lock:
                    found = await scan_for_probes(
                        timeout=self._config.scan_timeout, adapter=self._config.adapter
                    )
            except (BleakError, OSError) as exc:
                # Usually transient adapter contention; log it quietly and
                # come back, rather than filling the log every few seconds.
                _LOGGER.debug("scan failed: %s", exc)
                await self._sleep_or_stop(self._config.reconnect_delay)
                continue

            seen: set[str] = set()
            for device, adv in found:
                address = device.address.upper()
                seen.add(address)
                # Cache the device object: BlueZ will not connect by bare
                # address unless it has seen the device recently.
                self._devices[address] = device
                if adv.rssi is not None and adv.rssi > _RSSI_UNAVAILABLE:
                    self._rssi[address] = adv.rssi
                name = adv.local_name or device.name
                if name:
                    self._names[address] = name

            bound = set(self._assignments.values())
            unbound = [
                device for device, _ in found if device.address.upper() not in bound
            ]
            if unbound and not self._free_slots():
                # Only now does holding a slot for an absent probe cost
                # anything, so only now is it worth giving up.
                self._release_absent_slots(seen)
            await self._assign_free_slots(unbound)

            if self._free_slots():
                # Nothing advertising for the remaining slots.  A probe left
                # connected by a previous run is silent but still reachable,
                # so ask the adapter what it already knows.
                known = await known_probes_from_adapter(self._config.adapter)
                leftover = [
                    device
                    for device in known
                    if device.address.upper() not in self._assignments.values()
                ]
                if leftover:
                    _LOGGER.info(
                        "no probe is advertising; adopting %s from the adapter's "
                        "known devices (likely left connected by an earlier run)",
                        ", ".join(device.address for device in leftover),
                    )
                    await self._assign_free_slots(leftover)

            if self._needs_discovery():
                _LOGGER.debug(
                    "still looking for slot(s) %s; is the probe out of its "
                    "charger, or held by the MEATER phone app?",
                    self._pending_slots(),
                )
                await self._sleep_or_stop(self._config.reconnect_delay)

    def _release_absent_slots(self, seen: set[str]) -> None:
        """Free a remembered slot whose probe keeps not showing up.

        Only matters when another probe is waiting for a slot, so a probe that
        is present is never left out because of one that is not.
        """
        for index, address in list(self._assignments.items()):
            if index in self._pinned or index in self._connected:
                continue
            if address in seen:
                self._absent_scans.pop(index, None)
                continue
            misses = self._absent_scans.get(index, 0) + 1
            self._absent_scans[index] = misses
            if misses < _RELEASE_AFTER_SCANS:
                continue
            _LOGGER.info(
                "slot %d: %s has not answered in %d scans, freeing the slot",
                index,
                address,
                misses,
            )
            del self._assignments[index]
            self._absent_scans.pop(index, None)
            task = self._tasks.pop(index, None)
            if task is not None:
                task.cancel()

    async def _assign_free_slots(self, candidates: list[BLEDevice]) -> None:
        """Bind unassigned probes to free slots, strongest signal first."""
        for device in candidates:
            address = device.address.upper()
            if address in self._assignments.values():
                continue
            free = self._free_slots()
            if not free:
                return
            self._devices[address] = device
            await self._assign(free[0], address, device.name)
            self._ensure_connection_tasks()

    async def _sleep_or_stop(self, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    # -- per-probe connection ------------------------------------------------

    async def _probe_loop(self, index: int, address: str) -> None:
        backoff = self._config.reconnect_delay
        while not self._stopping.is_set():
            try:
                await self._connect_once(index, address)
                backoff = self._config.reconnect_delay
            except asyncio.CancelledError:
                raise
            except (BleakError, OSError, TimeoutError) as exc:
                _LOGGER.info("slot %d (%s): %s", index, address, exc)
                # The cached handle may be stale; let the next pass re-resolve.
                self._devices.pop(address, None)
            except Exception:
                _LOGGER.exception("slot %d (%s): unexpected failure", index, address)
            await _maybe_await(
                self._on_connection, index, address, False, self._names.get(address)
            )
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 1.6, _MAX_BACKOFF_S)

    async def _connect_once(self, index: int, address: str) -> None:
        # Prefer the device object discovery cached, but fall back to the bare
        # address: BlueZ still knows a device that has stopped advertising,
        # which is exactly the case after a crash leaves a probe connected.
        # Without this the server could never recover from an unclean exit,
        # because a connected probe does not advertise and so is never
        # rediscovered.
        target: BLEDevice | str = self._devices.get(address) or address

        kwargs: dict = {"adapter": self._config.adapter} if self._config.adapter else {}
        disconnected = asyncio.Event()

        def _on_disconnect(_client: BleakClient) -> None:
            disconnected.set()

        client = BleakClient(
            target, disconnected_callback=_on_disconnect, timeout=30.0, **kwargs
        )
        _LOGGER.debug("slot %d: connecting to %s", index, address)
        async with self._adapter_lock:
            if self._stopping.is_set():
                return
            await client.connect()

        try:
            _LOGGER.info("slot %d: connected to %s", index, address)
            self._connected.add(index)
            await _maybe_await(
                self._on_connection, index, address, True, self._names.get(address)
            )

            polled = await self._subscribe(client, index, address)
            # Seed an immediate reading so the UI is not blank for a whole
            # notification interval.
            await self._poll(
                client, index, address, {TEMPERATURE_CHAR_UUID, BATTERY_CHAR_UUID}
            )

            if polled:
                while not disconnected.is_set() and not self._stopping.is_set():
                    try:
                        await asyncio.wait_for(
                            disconnected.wait(), timeout=_POLL_INTERVAL_S
                        )
                    except TimeoutError:
                        await self._poll(client, index, address, polled)
            else:
                waiters = [
                    asyncio.create_task(disconnected.wait()),
                    asyncio.create_task(self._stopping.wait()),
                ]
                try:
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waiters:
                        waiter.cancel()
        finally:
            self._connected.discard(index)
            # A stale cache entry is worse than none: on the next attempt the
            # discovery loop will supply a fresh one.
            self._devices.pop(address, None)
            with contextlib.suppress(BleakError, OSError, TimeoutError):
                await client.disconnect()

        _LOGGER.info("slot %d: disconnected from %s", index, address)

    async def _subscribe(
        self, client: BleakClient, index: int, address: str
    ) -> set[str]:
        """Subscribe where possible; return the UUIDs that must be polled."""
        must_poll: set[str] = set()
        for uuid in (TEMPERATURE_CHAR_UUID, BATTERY_CHAR_UUID):
            char = client.services.get_characteristic(uuid)
            if char is None:
                _LOGGER.warning(
                    "slot %d: %s exposes no characteristic %s", index, address, uuid
                )
                continue
            if "notify" not in char.properties and "indicate" not in char.properties:
                must_poll.add(uuid)
                continue
            try:
                await client.start_notify(
                    char,
                    lambda _char, data, uuid=uuid: self._handle_payload(
                        index, address, uuid, bytes(data)
                    ),
                )
            except (BleakError, OSError) as exc:
                _LOGGER.info(
                    "slot %d: could not subscribe to %s (%s); polling instead",
                    index,
                    uuid,
                    exc,
                )
                must_poll.add(uuid)
        return must_poll

    async def _poll(
        self, client: BleakClient, index: int, address: str, uuids: set[str]
    ) -> None:
        for uuid in uuids:
            char = client.services.get_characteristic(uuid)
            if char is None or "read" not in char.properties:
                continue
            try:
                raw = await client.read_gatt_char(char)
            except (BleakError, OSError) as exc:
                _LOGGER.debug("slot %d: read of %s failed: %s", index, uuid, exc)
                continue
            self._handle_payload(index, address, uuid, bytes(raw))

    def _handle_payload(self, index: int, address: str, uuid: str, data: bytes) -> None:
        """Decode one characteristic payload and dispatch any new reading."""
        try:
            if uuid == BATTERY_CHAR_UUID:
                self._battery[address] = decode_battery(data)
                return
            tip_c, ambient_c = decode_temperatures(data)
        except DecodeError as exc:
            _LOGGER.debug("slot %d: %s (payload %s)", index, exc, data.hex(" "))
            return

        if not (is_plausible_celsius(tip_c) and is_plausible_celsius(ambient_c)):
            _LOGGER.debug(
                "slot %d: discarding implausible reading tip=%.1f ambient=%.1f (%s)",
                index,
                tip_c,
                ambient_c,
                data.hex(" "),
            )
            return

        reading = Reading(
            tip_c=tip_c,
            ambient_c=ambient_c,
            battery_pct=self._battery.get(address),
            wall_time=time.time(),
            monotonic=time.monotonic(),
            rssi=self._rssi.get(address),
        )
        result = self._on_reading(index, address, reading)
        if asyncio.iscoroutine(result):
            asyncio.get_running_loop().create_task(result)


async def _maybe_await(callback, *args) -> None:
    if callback is None:
        return
    result = callback(*args)
    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
        await result
