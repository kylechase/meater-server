"""ntfy alerts: URL parsing, and the pushes a cook triggers."""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp import web

from meater_server.config import Config
from meater_server.manager import MeaterManager
from meater_server.notify import split_topic_url
from meater_server.state import CookState, Reading


def reading(tip: float, ambient: float = 160.0) -> Reading:
    return Reading(
        tip_c=tip,
        ambient_c=ambient,
        battery_pct=80,
        wall_time=time.time(),
        monotonic=time.monotonic(),
        rssi=-55,
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ntfy.example.com/alerts", ("https://ntfy.example.com", "alerts")),
        ("ntfy.example.com/alerts/", ("https://ntfy.example.com", "alerts")),
        ("http://host:8000/ntfy/grill", ("http://host:8000/ntfy", "grill")),
    ],
)
def test_split_topic_url(url, expected):
    assert split_topic_url(url) == expected


def test_url_without_topic_is_refused():
    with pytest.raises(ValueError):
        split_topic_url("https://ntfy.example.com/")


def test_config_reads_notify_section():
    config = Config.from_mapping({"notify": {"ntfy_url": "https://n.example/t"}})
    assert config.notify_ntfy_url == "https://n.example/t"


async def test_cook_pushes_ready_and_done(aiohttp_server, config: Config):
    received: list[dict] = []
    got = asyncio.Event()

    async def publish(request: web.Request) -> web.Response:
        received.append(await request.json())
        got.set()
        return web.json_response({"id": "x"})

    app = web.Application()
    app.router.add_post("/", publish)
    server = await aiohttp_server(app)

    config.notify_ntfy_url = f"http://127.0.0.1:{server.port}/grill"
    config.units = "F"
    manager = MeaterManager(config)
    slot = manager.slot(1)

    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(20.0))
    manager.start_cook(slot, "chicken_breast")
    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(50.0))
    await manager.notifier.drain()
    assert received == []  # still cooking: nothing sent

    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(80.0))
    await asyncio.wait_for(got.wait(), 5)
    assert slot.cook.state is CookState.READY
    ready = received[0]
    assert ready["topic"] == "grill"
    assert ready["priority"] == 5
    assert "ready" in ready["title"]
    assert "176°F" in ready["message"]

    # Further readings past the pull temperature do not repeat the alert.
    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(81.0))
    await manager.notifier.drain()
    assert len(received) == 1

    got.clear()
    manager.start_resting(slot)
    slot.cook.rest_started_at = time.time() - 24 * 3600
    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(70.0))
    await asyncio.wait_for(got.wait(), 5)
    assert slot.cook.state is CookState.DONE
    assert "resting" in received[1]["title"]


async def test_unreachable_ntfy_does_not_break_the_cook(config: Config):
    config.notify_ntfy_url = "http://127.0.0.1:9/grill"  # discard port: refused
    manager = MeaterManager(config)
    slot = manager.slot(1)
    manager.start_cook(slot, "chicken_breast")
    manager.on_reading(1, "AA:BB:CC:DD:EE:01", reading(80.0))
    await manager.notifier.drain()
    assert slot.cook.state is CookState.READY
