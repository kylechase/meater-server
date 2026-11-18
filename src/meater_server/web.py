"""The local web UI and its JSON API.

Served on the same event loop as everything else.  State reaches the browser
over server-sent events, which need no handshake, reconnect themselves, and
work fine through the kind of flaky phone wifi you get standing next to a
grill.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from importlib import resources

from aiohttp import web

from .manager import MeaterManager
from .presets import CUSTOM_PRESET_ID
from .state import ProbeSlot

_LOGGER = logging.getLogger(__name__)

#: Keeps proxies and phone radios from dropping an idle event stream.
_HEARTBEAT_S = 20.0


def build_app(manager: MeaterManager) -> web.Application:
    app = web.Application()
    app["manager"] = manager
    app.add_routes(
        [
            web.get("/", _index),
            web.get("/healthz", _healthz),
            web.get("/api/state", _state),
            web.get("/api/presets", _presets),
            web.get("/api/events", _events),
            web.post("/api/probe/{index}/cook", _start_cook),
            web.delete("/api/probe/{index}/cook", _clear_cook),
            web.post("/api/probe/{index}/target", _set_target),
            web.post("/api/probe/{index}/rest", _start_rest),
            web.post("/api/units", _set_units),
            web.get("/{asset:(app\\.js|style\\.css|icon\\.svg|manifest\\.webmanifest)}", _asset),
        ]
    )
    return app


#: Served assets, and the content type each goes out with.
_ASSET_TYPES = {
    "app.js": "text/javascript",
    "style.css": "text/css",
    "icon.svg": "image/svg+xml",
    "manifest.webmanifest": "application/manifest+json",
    "index.html": "text/html",
}


def _static_text(name: str) -> str:
    """Read a bundled asset.  ``static`` is package data, not a package."""
    return (
        resources.files("meater_server").joinpath("static", name).read_text(encoding="utf-8")
    )


async def _index(_request: web.Request) -> web.StreamResponse:
    return web.Response(text=_static_text("index.html"), content_type="text/html")


async def _asset(request: web.Request) -> web.StreamResponse:
    name = request.match_info["asset"]
    return web.Response(
        text=_static_text(name),
        content_type=_ASSET_TYPES[name],
        headers={"Cache-Control": "no-cache"},
    )


async def _healthz(_request: web.Request) -> web.StreamResponse:
    return web.json_response({"ok": True})


async def _state(request: web.Request) -> web.StreamResponse:
    return web.json_response(_manager(request).snapshot())


async def _presets(request: web.Request) -> web.StreamResponse:
    manager = _manager(request)
    return web.json_response(
        {"units": manager.config.units, "categories": manager.preset_catalog()}
    )


async def _events(request: web.Request) -> web.StreamResponse:
    """Stream snapshots as server-sent events."""
    manager = _manager(request)
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)

    queue = manager.subscribe()
    try:
        await _send_event(response, json.dumps(manager.snapshot()))
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                await response.write(b": keepalive\n\n")
                continue
            await _send_event(response, payload)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        manager.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await response.write_eof()
    return response


async def _send_event(response: web.StreamResponse, payload: str) -> None:
    await response.write(f"data: {payload}\n\n".encode())


# -- commands --------------------------------------------------------------


async def _start_cook(request: web.Request) -> web.StreamResponse:
    manager = _manager(request)
    slot = _slot(request)
    body = await _json_body(request)

    preset_id = body.get("preset_id")
    if not isinstance(preset_id, str):
        raise web.HTTPBadRequest(reason="preset_id is required")

    target = body.get("target")
    target_c = None
    if target is not None:
        target_c = manager.target_to_celsius(_as_float(target, "target"))

    doneness_id = body.get("doneness_id")
    if doneness_id is not None and not isinstance(doneness_id, str):
        raise web.HTTPBadRequest(reason="doneness_id must be a string")

    if not manager.start_cook(slot, preset_id, doneness_id, target_c):
        raise web.HTTPNotFound(reason=f"unknown preset {preset_id!r}")
    return web.json_response(slot.to_dict(manager.library, manager.config.units))


async def _clear_cook(request: web.Request) -> web.StreamResponse:
    manager = _manager(request)
    slot = _slot(request)
    manager.clear_cook(slot)
    return web.json_response(slot.to_dict(manager.library, manager.config.units))


async def _set_target(request: web.Request) -> web.StreamResponse:
    manager = _manager(request)
    slot = _slot(request)
    body = await _json_body(request)
    target_c = manager.target_to_celsius(_as_float(body.get("target"), "target"))
    if not 0 < target_c <= 150:
        raise web.HTTPBadRequest(reason="target is out of range")
    if slot.cook is None:
        manager.start_cook(slot, CUSTOM_PRESET_ID, target_c=target_c)
    else:
        manager.set_target(slot, target_c)
    return web.json_response(slot.to_dict(manager.library, manager.config.units))


async def _start_rest(request: web.Request) -> web.StreamResponse:
    manager = _manager(request)
    slot = _slot(request)
    if slot.cook is None:
        raise web.HTTPConflict(reason="no cook in progress")
    manager.start_resting(slot)
    return web.json_response(slot.to_dict(manager.library, manager.config.units))


async def _set_units(request: web.Request) -> web.StreamResponse:
    manager = _manager(request)
    body = await _json_body(request)
    units = str(body.get("units", "")).upper()
    if units not in ("C", "F"):
        raise web.HTTPBadRequest(reason="units must be C or F")
    manager.config.units = units
    manager._broadcast()
    return web.json_response({"units": units})


# -- helpers ---------------------------------------------------------------


def _manager(request: web.Request) -> MeaterManager:
    return request.app["manager"]


def _slot(request: web.Request) -> ProbeSlot:
    try:
        index = int(request.match_info["index"])
    except (KeyError, ValueError) as exc:
        raise web.HTTPBadRequest(reason="probe index must be a number") from exc
    slot = _manager(request).slot(index)
    if slot is None:
        raise web.HTTPNotFound(reason=f"no probe slot {index}")
    return slot


async def _json_body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason="body must be JSON") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="body must be a JSON object")
    return body


def _as_float(value: object, field: str) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason=f"{field} must be a number") from exc


async def start_web(manager: MeaterManager) -> web.AppRunner:
    """Start the web server and return its runner, for later cleanup."""
    app = build_app(manager)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, manager.config.web_host, manager.config.web_port)
    await site.start()
    _LOGGER.info(
        "web UI on http://%s:%d",
        manager.config.web_host,
        manager.config.web_port,
    )
    return runner
