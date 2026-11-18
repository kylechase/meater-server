"""End-to-end: drive the server with Home Assistant's own ESPHome client.

aioesphomeapi's ``APIClient`` is exactly what the Home Assistant integration
uses, so if it can connect, enumerate entities, receive states and send
commands, the native API implementation is correct in the way that matters.
"""

from __future__ import annotations

import asyncio
import base64
import time

import pytest
from aioesphomeapi import APIClient
from aioesphomeapi.model import (
    BinarySensorInfo,
    ButtonInfo,
    NumberInfo,
    SelectInfo,
    SensorInfo,
    TextSensorInfo,
)

from meater_server.config import Config
from meater_server.manager import MeaterManager
from meater_server.state import CookState, Reading
from meater_server.web import build_app

pytestmark = pytest.mark.asyncio


def reading(tip: float, ambient: float = 160.0, battery: int = 80) -> Reading:
    now = time.time()
    return Reading(
        tip_c=tip,
        ambient_c=ambient,
        battery_pct=battery,
        wall_time=now,
        monotonic=time.monotonic(),
        rssi=-55,
    )


class Harness:
    """A running manager with its ESPHome server up, but no Bluetooth."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.manager = MeaterManager(config)

    async def __aenter__(self) -> Harness:
        self.manager.load_state()
        assert self.manager.esphome is not None
        await self.manager.esphome.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self.manager.esphome is not None
        await self.manager.esphome.stop()

    def client(self, noise_psk: str | None = None) -> APIClient:
        return APIClient(
            "127.0.0.1",
            self.config.esphome_port,
            None,
            client_info="pytest",
            noise_psk=noise_psk,
            expected_name=self.config.esphome_name,
        )


async def test_client_connects_and_reads_device_info(config: Config):
    async with Harness(config) as harness:
        client = harness.client()
        await client.connect(login=True)
        try:
            info = await client.device_info()
            assert info.name == config.esphome_name
            assert info.friendly_name == config.esphome_friendly_name
            assert len(info.mac_address) == 17
            assert "meater-server" in info.esphome_version
            # Reported deliberately: at 1.15+ the client would demand a
            # DeviceCapabilitiesRequest RPC this device does not implement.
            assert (client.api_version.major, client.api_version.minor) == (1, 10)
        finally:
            await client.disconnect()


async def test_entity_list_covers_every_slot(config: Config):
    async with Harness(config) as harness:
        client = harness.client()
        await client.connect(login=True)
        try:
            entities, services = await client.list_entities_services()
            assert services == []

            by_type: dict[type, list] = {}
            for entity in entities:
                by_type.setdefault(type(entity), []).append(entity)

            # Two slots, so two of everything.
            assert len(by_type[SelectInfo]) == 2
            assert len(by_type[NumberInfo]) == 2
            assert len(by_type[BinarySensorInfo]) == 2
            assert len(by_type[ButtonInfo]) == 4  # rest + clear, per slot
            assert len(by_type[TextSensorInfo]) == 6
            assert len(by_type[SensorInfo]) == 20

            # Keys must be unique, or Home Assistant would collapse entities.
            keys = [e.key for e in entities]
            assert len(keys) == len(set(keys))

            tip = next(e for e in by_type[SensorInfo] if e.object_id == "probe_1_tip_temperature")
            assert tip.unit_of_measurement == "°C"
            assert tip.device_class == "temperature"
            assert tip.accuracy_decimals == 1

            select = next(e for e in by_type[SelectInfo] if e.object_id == "probe_1_cook_preset")
            assert select.options[0] == "None"
            assert "Beef - Steak - Medium rare" in select.options
        finally:
            await client.disconnect()


async def test_states_stream_and_commands_round_trip(config: Config):
    async with Harness(config) as harness:
        manager = harness.manager
        client = harness.client()
        await client.connect(login=True)
        try:
            entities, _ = await client.list_entities_services()
            keys = {e.object_id: e.key for e in entities}

            states: dict[int, object] = {}
            updated = asyncio.Event()

            def on_state(state) -> None:
                states[state.key] = state
                updated.set()

            client.subscribe_states(on_state)

            async def settle() -> None:
                """Wait for the next push, then let the rest of it arrive."""
                updated.clear()
                await asyncio.wait_for(updated.wait(), timeout=5)
                await asyncio.sleep(0.05)

            await settle()

            # A reading arrives over Bluetooth and shows up in Home Assistant.
            manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(21.5))
            await settle()
            assert states[keys["probe_1_tip_temperature"]].state == pytest.approx(21.5)
            assert states[keys["probe_1_ambient_temperature"]].state == pytest.approx(160.0)
            assert states[keys["probe_1_battery"]].state == pytest.approx(80)
            assert states[keys["probe_1_connected"]].state is True
            assert states[keys["probe_1_cook"]].state == "Nothing"
            assert states[keys["probe_1_cook_state"]].state == "idle"

            # Home Assistant picks a cook from the select entity.
            client.select_command(keys["probe_1_cook_preset"], "Beef - Steak - Medium rare")
            await settle()
            slot = manager.slot(1)
            assert slot is not None
            assert slot.cook is not None
            assert slot.cook.preset_id == "beef_steak"
            assert slot.cook.doneness_id == "medium_rare"
            assert states[keys["probe_1_cook"]].state == "Beef Steak, Medium rare"
            assert states[keys["probe_1_target_temperature"]].state == pytest.approx(57.2)
            # Steak carries 2 C of carryover, so it comes off 2 C early.
            assert states[keys["probe_1_pull_temperature"]].state == pytest.approx(55.2)

            # Overriding the target from the number entity.
            client.number_command(keys["probe_1_target_override"], 70.0)
            await settle()
            assert slot.cook.target_c == pytest.approx(70.0)

            # Reaching the pull temperature flips the state to "ready".
            manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(71.0))
            await settle()
            assert slot.cook.state is CookState.READY
            assert states[keys["probe_1_cook_state"]].state == "ready"
            assert states[keys["probe_1_time_remaining"]].state == pytest.approx(0)

            # The resting button, then clearing the cook.
            client.button_command(keys["probe_1_start_resting"])
            await settle()
            assert slot.cook.state is CookState.RESTING

            client.button_command(keys["probe_1_clear_cook"])
            await settle()
            assert slot.cook is None
            assert states[keys["probe_1_cook_state"]].state == "idle"

            # Slot 2 never saw a probe, so its states report as missing rather
            # than as a plausible-looking zero.
            assert states[keys["probe_2_tip_temperature"]].missing_state is True
            assert states[keys["probe_2_connected"]].state is False
        finally:
            await client.disconnect()


async def test_encrypted_connection(config: Config):
    config.esphome_encryption_key = base64.b64encode(bytes(range(32))).decode()
    async with Harness(config) as harness:
        client = harness.client(noise_psk=config.esphome_encryption_key)
        await client.connect(login=True)
        try:
            info = await client.device_info()
            assert info.name == config.esphome_name
            assert info.api_encryption_supported is True
        finally:
            await client.disconnect()


async def test_encrypted_endpoint_rejects_the_wrong_key(config: Config):
    config.esphome_encryption_key = base64.b64encode(bytes(range(32))).decode()
    async with Harness(config) as harness:
        wrong = base64.b64encode(bytes(range(32, 64))).decode()
        client = harness.client(noise_psk=wrong)
        with pytest.raises(Exception) as caught:
            await client.connect(login=True)
        assert "key" in str(caught.value).lower() or "handshake" in str(caught.value).lower()


async def test_two_clients_at_once(config: Config):
    """Home Assistant plus the esphome CLI should both be able to watch."""
    async with Harness(config) as harness:
        first, second = harness.client(), harness.client()
        await first.connect(login=True)
        await second.connect(login=True)
        try:
            assert (await first.device_info()).name == config.esphome_name
            assert (await second.device_info()).name == config.esphome_name
            entities_a, _ = await first.list_entities_services()
            entities_b, _ = await second.list_entities_services()
            assert {e.key for e in entities_a} == {e.key for e in entities_b}
        finally:
            await first.disconnect()
            await second.disconnect()


# --- web API --------------------------------------------------------------


async def test_web_api_drives_a_cook(aiohttp_client, config: Config):
    manager = MeaterManager(config)
    client = await aiohttp_client(build_app(manager))

    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(18.0))

    response = await client.get("/api/state")
    assert response.status == 200
    snapshot = await response.json()
    assert snapshot["probes"][0]["tip"] == pytest.approx(18.0)
    assert snapshot["probes"][0]["cook"] is None

    response = await client.get("/api/presets")
    catalog = await response.json()
    assert any(group["category"] == "Beef" for group in catalog["categories"])

    response = await client.post(
        "/api/probe/1/cook", json={"preset_id": "chicken_breast"}
    )
    assert response.status == 200
    body = await response.json()
    assert body["cook"]["label"] == "Chicken Breast"
    assert body["cook"]["target"] == pytest.approx(73.9)

    # Unknown presets and bad bodies are refused, not silently accepted.
    assert (await client.post("/api/probe/1/cook", json={"preset_id": "nope"})).status == 404
    assert (await client.post("/api/probe/1/cook", json={})).status == 400
    assert (await client.post("/api/probe/9/rest")).status == 404
    assert (await client.post("/api/probe/1/target", json={"target": 999})).status == 400

    response = await client.post("/api/probe/1/target", json={"target": 68})
    assert response.status == 200
    assert (await response.json())["cook"]["target"] == pytest.approx(68.0)

    assert (await client.post("/api/probe/1/rest")).status == 200
    assert manager.slot(1).cook.state is CookState.RESTING

    assert (await client.delete("/api/probe/1/cook")).status == 200
    assert manager.slot(1).cook is None


async def test_web_api_switches_units(aiohttp_client, config: Config):
    manager = MeaterManager(config)
    client = await aiohttp_client(build_app(manager))
    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(100.0))

    assert (await client.post("/api/units", json={"units": "F"})).status == 200
    snapshot = await (await client.get("/api/state")).json()
    assert snapshot["units"] == "F"
    assert snapshot["probes"][0]["tip"] == pytest.approx(212.0)

    # A target typed into a Fahrenheit UI is read as Fahrenheit.
    await client.post("/api/probe/1/cook", json={"preset_id": "custom", "target": 150})
    assert manager.slot(1).cook.target_c == pytest.approx(65.56, abs=0.01)

    assert (await client.post("/api/units", json={"units": "K"})).status == 400


async def test_web_serves_the_ui_and_its_assets(aiohttp_client, config: Config):
    client = await aiohttp_client(build_app(MeaterManager(config)))
    for path, needle in (
        ("/", "<title>MEATER</title>"),
        ("/app.js", "EventSource"),
        ("/style.css", "--series-tip"),
        ("/manifest.webmanifest", "standalone"),
        ("/icon.svg", "<svg"),
    ):
        response = await client.get(path)
        assert response.status == 200, path
        assert needle in await response.text(), path


async def test_event_stream_pushes_updates(aiohttp_client, config: Config):
    manager = MeaterManager(config)
    client = await aiohttp_client(build_app(manager))

    response = await client.get("/api/events")
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    # The stream opens with a full snapshot so a fresh page is never blank.
    chunk = await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout=5)
    assert chunk.startswith(b"data: ")

    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(42.0))
    manager._broadcast()
    chunk = await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout=5)
    assert b'"tip": 42.0' in chunk
