"""The core: owns the slots, drives the cook state machine, fans out updates.

Readings arrive from :mod:`.ble`, commands arrive from the web UI or from Home
Assistant through :mod:`.esphome`, and both end up here.  Everything that
changes state goes through one of the :class:`SlotCommands` methods or
:meth:`MeaterManager.on_reading`, which keeps the state machine in a single
readable place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterator

from .ble import BleManager
from .config import Config
from .decode import f_to_c
from .esphome.entities import NO_COOK_OPTION, SlotCommands, build_entities
from .esphome.server import ESPHomeServer
from .notify import PRIORITY_HIGH, PRIORITY_URGENT, NtfyNotifier
from .presets import CUSTOM_PRESET_ID, PresetLibrary
from .state import CookState, ProbeSlot, Reading, build_session

_LOGGER = logging.getLogger(__name__)

#: How often to re-evaluate timers and republish, so countdowns keep moving
#: even when no new reading has arrived.
TICK_INTERVAL_S = 5.0
_STATE_FILE_VERSION = 1


class MeaterManager(SlotCommands):
    """Everything the server knows, and the only place it changes."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.library = PresetLibrary.from_config(config.presets)
        super().__init__(self.library)

        self.slots: dict[int, ProbeSlot] = {
            index: ProbeSlot(index) for index in range(1, config.slots + 1)
        }
        self.entities = build_entities(config.slots, self.library, self)

        self.ble = BleManager(
            config,
            on_reading=self.on_reading,
            on_connection=self.on_connection,
            on_assign=self.on_assign,
        )
        self.esphome: ESPHomeServer | None = None
        if config.esphome_enabled:
            self.esphome = ESPHomeServer(
                config, self.entities, self, self.slots.get
            )

        self.notifier: NtfyNotifier | None = None
        if config.notify_ntfy_url:
            self.notifier = NtfyNotifier(config.notify_ntfy_url, config.notify_ntfy_token)

        self._subscribers: set[asyncio.Queue[str]] = set()
        self._stopping = asyncio.Event()
        self._dirty = False

    # -- slot access ---------------------------------------------------------

    def slot(self, index: int) -> ProbeSlot | None:
        return self.slots.get(index)

    def __iter__(self) -> Iterator[ProbeSlot]:
        return iter(self.slots[i] for i in sorted(self.slots))

    # -- BLE callbacks -------------------------------------------------------

    def on_reading(self, index: int, address: str, reading: Reading) -> None:
        slot = self.slots.get(index)
        if slot is None:
            return
        slot.address = address
        slot.connected = True
        slot.reading = reading
        slot.last_seen = reading.wall_time
        slot.record_graph_point(reading)
        slot.estimator.add(reading.monotonic, reading.tip_c, reading.ambient_c)
        self._advance_cook(slot)
        self._changed(index)

    def on_connection(
        self, index: int, address: str, connected: bool, name: str | None
    ) -> None:
        slot = self.slots.get(index)
        if slot is None:
            return
        slot.address = address
        slot.connected = connected
        if name:
            slot.name = name
        if not connected:
            # Keep the last reading on screen, but stop estimating from a
            # history that is about to have a hole in it.
            slot.estimator.reset()
        self._changed(index)

    def on_assign(self, index: int, address: str, name: str | None) -> None:
        slot = self.slots.get(index)
        if slot is None:
            return
        slot.address = address
        if name:
            slot.name = name
        self.save_state()
        self._changed(index)

    # -- cook state machine --------------------------------------------------

    def _advance_cook(self, slot: ProbeSlot) -> None:
        cook = slot.cook
        if cook is None:
            return

        if slot.reading is not None:
            tip = slot.reading.tip_c
            cook.peak_tip_c = tip if cook.peak_tip_c is None else max(cook.peak_tip_c, tip)
            if cook.state is CookState.COOKING and tip >= cook.pull_c:
                cook.state = CookState.READY
                cook.ready_at = time.time()
                _LOGGER.info(
                    "slot %d: %s reached %.1f C - ready to remove",
                    slot.index,
                    cook.label,
                    tip,
                )
                self.save_state()
                self._alert(
                    f"{cook.label} is ready",
                    f"{slot.display_name} hit {self._fmt_temp(tip)} "
                    f"(pull {self._fmt_temp(cook.pull_c)}). Take it off the heat.",
                    PRIORITY_URGENT,
                    ["rotating_light", "cut_of_meat"],
                )

        if cook.state is CookState.RESTING and not cook.rest_remaining_s:
            cook.state = CookState.DONE
            _LOGGER.info("slot %d: %s has finished resting", slot.index, cook.label)
            self.save_state()
            self._alert(
                f"{cook.label} is done resting",
                f"{slot.display_name}: time to serve.",
                PRIORITY_HIGH,
                ["white_check_mark", "fork_and_knife"],
            )

    def _alert(
        self, title: str, message: str, priority: int, tags: list[str]
    ) -> None:
        if self.notifier is not None:
            self.notifier.send(title, message, priority, tags)

    def _fmt_temp(self, celsius: float) -> str:
        if self.config.units == "F":
            return f"{celsius * 9 / 5 + 32:.0f}\u00b0F"
        return f"{celsius:.1f}\u00b0C"

    # -- SlotCommands --------------------------------------------------------

    def select_preset(self, slot: ProbeSlot, option: str) -> None:
        """Start a cook from a select-option label, or clear it."""
        if not option or option == NO_COOK_OPTION:
            self.clear_cook(slot)
            return
        resolved = self.resolve_option(option)
        if resolved is None:
            _LOGGER.warning("slot %d: unknown cook option %r", slot.index, option)
            return
        self.start_cook(slot, *resolved)

    def start_cook(
        self,
        slot: ProbeSlot,
        preset_id: str,
        doneness_id: str | None = None,
        target_c: float | None = None,
    ) -> bool:
        """Begin a cook on ``slot``.  Returns False for an unknown preset."""
        preset = self.library.get(preset_id)
        if preset is None:
            _LOGGER.warning("slot %d: unknown preset %r", slot.index, preset_id)
            return False
        doneness = preset.doneness(doneness_id)
        slot.cook = build_session(preset, doneness, target_c)
        slot.estimator.reset()
        slot.graph.clear()
        if slot.reading is not None:
            # Seed the fresh estimator so the graph and the fit start from the
            # temperature the meat is actually at.
            slot.estimator.add(
                slot.reading.monotonic, slot.reading.tip_c, slot.reading.ambient_c
            )
            slot.record_graph_point(slot.reading)
        _LOGGER.info(
            "slot %d: cooking %s to %.1f C (pull at %.1f C, rest %.0f min)",
            slot.index,
            slot.cook.label,
            slot.cook.target_c,
            slot.cook.pull_c,
            slot.cook.rest_minutes,
        )
        self._advance_cook(slot)
        self.save_state()
        self._changed(slot.index)
        return True

    def set_target(self, slot: ProbeSlot, target_c: float) -> None:
        """Override the target temperature, starting a custom cook if needed."""
        if target_c <= 0:
            return
        if slot.cook is None:
            self.start_cook(slot, CUSTOM_PRESET_ID, target_c=target_c)
            return
        slot.cook.target_c = float(target_c)
        slot.cook.custom_target = True
        if slot.cook.state in (CookState.READY, CookState.DONE):
            # Raising the target puts the cook back on the heat.
            slot.cook.state = CookState.COOKING
            slot.cook.ready_at = None
        self._advance_cook(slot)
        self.save_state()
        self._changed(slot.index)

    def start_resting(self, slot: ProbeSlot) -> None:
        if slot.cook is None:
            return
        slot.cook.state = CookState.RESTING
        slot.cook.rest_started_at = time.time()
        _LOGGER.info(
            "slot %d: resting %s for %.0f min",
            slot.index,
            slot.cook.label,
            slot.cook.rest_minutes,
        )
        self._advance_cook(slot)
        self.save_state()
        self._changed(slot.index)

    def clear_cook(self, slot: ProbeSlot) -> None:
        if slot.cook is not None:
            _LOGGER.info("slot %d: cleared %s", slot.index, slot.cook.label)
        slot.cook = None
        slot.estimator.reset()
        slot.graph.clear()
        self.save_state()
        self._changed(slot.index)

    # -- change fan-out ------------------------------------------------------

    def _changed(self, slot_index: int | None = None) -> None:
        """Publish to Home Assistant now, and to web clients on the next tick."""
        if self.esphome is not None:
            self.esphome.publish_states(slot_index)
        self._dirty = True

    def subscribe(self) -> asyncio.Queue[str]:
        """A queue of serialised snapshots, for the web UI's event stream."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def _broadcast(self) -> None:
        if not self._subscribers:
            return
        payload = json.dumps(self.snapshot())
        for queue in list(self._subscribers):
            if queue.full():
                # A client that cannot keep up gets the newest snapshot, not a
                # backlog of stale ones.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    # -- snapshots -----------------------------------------------------------

    def snapshot(self) -> dict:
        """The whole visible state, for the web UI."""
        return {
            "now": time.time(),
            "units": self.config.units,
            "probes": [
                slot.to_dict(self.library, self.config.units) for slot in self
            ],
        }

    def preset_catalog(self) -> list[dict]:
        """The preset table, grouped for the UI's picker."""
        out = []
        for category, presets in self.library.by_category().items():
            out.append(
                {
                    "category": category,
                    "presets": [
                        {
                            "id": preset.id,
                            "label": preset.label,
                            "note": preset.note,
                            "rest_minutes": preset.rest_minutes,
                            "carryover_c": preset.carryover_c,
                            "donenesses": [
                                {
                                    "id": d.id,
                                    "label": d.label,
                                    "target_c": round(d.target_c, 1),
                                    "target_f": round(d.target_c * 9 / 5 + 32),
                                }
                                for d in preset.donenesses
                            ],
                        }
                        for preset in presets
                    ],
                }
            )
        return out

    def target_to_celsius(self, value: float) -> float:
        """Interpret a target from the UI in whatever unit the UI is using."""
        return f_to_c(value) if self.config.units == "F" else value

    # -- persistence ---------------------------------------------------------

    def load_state(self) -> None:
        """Restore slot assignments and any cook that was running."""
        assignments: dict[int, str] = {
            index + 1: address for index, address in enumerate(self.config.addresses)
        }
        # Config-supplied slots are held for that probe no matter what; slots
        # restored from the state file are only a preference.
        pinned = set(assignments)

        path = self.config.state_file
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            _LOGGER.warning("could not read %s: %s", path, exc)
            raw = {}

        for entry in raw.get("slots", []) if isinstance(raw, dict) else []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry["index"])
            except (KeyError, TypeError, ValueError):
                continue
            slot = self.slots.get(index)
            if slot is None:
                continue
            address = entry.get("address")
            # A configured address always wins over a remembered one.
            if isinstance(address, str) and index not in assignments:
                assignments[index] = address.upper()
                slot.address = address.upper()
            if isinstance(entry.get("name"), str):
                slot.name = entry["name"]
            cook = entry.get("cook")
            if isinstance(cook, dict):
                from .state import CookSession

                restored = CookSession.from_dict(cook)
                if restored is not None:
                    slot.cook = restored
                    _LOGGER.info(
                        "slot %d: resumed %s (%s)", index, restored.label, restored.state
                    )

        self.ble.seed_assignments(assignments, pinned)
        for index, address in assignments.items():
            slot = self.slots.get(index)
            if slot is not None:
                slot.address = address

    def save_state(self) -> None:
        """Write slot assignments and cooks, so a restart resumes the cook."""
        path = self.config.state_file
        payload = {
            "version": _STATE_FILE_VERSION,
            "saved_at": time.time(),
            "slots": [
                {
                    "index": slot.index,
                    "address": slot.address,
                    "name": slot.name,
                    "cook": slot.cook.to_dict() if slot.cook else None,
                }
                for slot in self
            ],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2))
            temporary.replace(path)
        except OSError as exc:
            _LOGGER.warning("could not write %s: %s", path, exc)

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Run the BLE manager, the ESPHome server, and the timer tick."""
        self.load_state()
        if self.esphome is not None:
            await self.esphome.start()

        tasks = [
            asyncio.create_task(self.ble.run(), name="meater-ble"),
            asyncio.create_task(self._tick_loop(), name="meater-tick"),
        ]
        try:
            await self._stopping.wait()
        finally:
            self.ble.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.esphome is not None:
                await self.esphome.stop()
            if self.notifier is not None:
                await self.notifier.drain()
            self.save_state()

    def stop(self) -> None:
        self._stopping.set()

    async def _tick_loop(self) -> None:
        """Advance timers, expire stale probes, and push updates."""
        while not self._stopping.is_set():
            now = time.time()
            for slot in self:
                if (
                    slot.connected
                    and slot.last_seen is not None
                    and now - slot.last_seen > self.config.stale_after
                ):
                    _LOGGER.info(
                        "slot %d: no reading for %.0fs, marking disconnected",
                        slot.index,
                        now - slot.last_seen,
                    )
                    slot.connected = False
                    self._dirty = True
                self._advance_cook(slot)

            # Countdowns move with the clock, so republish on every tick even
            # when nothing else changed; both sinks de-duplicate.
            if self.esphome is not None:
                self.esphome.publish_states()
            self._broadcast()
            self._dirty = False

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_INTERVAL_S)
