"""An ESPHome native API server, so Home Assistant sees the probes as a device.

The native API is a protobuf stream on TCP 6053.  The message definitions
come from :mod:`aioesphomeapi`, the same library Home Assistant uses as the
client, so the two cannot drift apart.

The server reports API version 1.10 deliberately.  From 1.15 the client
switches to a separate ``DeviceCapabilitiesRequest`` RPC for proxy and voice
features this device does not have; below that it synthesises capabilities
from ``DeviceInfoResponse``, which is exactly right here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from aioesphomeapi import api_pb2 as pb

from .. import __version__
from ..config import Config
from ..state import ProbeSlot
from .entities import Entity, Kind, SlotCommands
from .frames import (
    FrameError,
    NoiseFramer,
    PlaintextFramer,
    RequiresEncryption,
    decode_noise_psk,
)

if TYPE_CHECKING:
    from zeroconf import Zeroconf
    from zeroconf.asyncio import AsyncServiceInfo

_LOGGER = logging.getLogger(__name__)

API_VERSION_MAJOR = 1
API_VERSION_MINOR = 10

# Message type numbers, from ESPHome's api.proto.
MSG_HELLO_REQUEST = 1
MSG_HELLO_RESPONSE = 2
MSG_LEGACY_CONNECT_REQUEST = 3
MSG_LEGACY_CONNECT_RESPONSE = 4
MSG_DISCONNECT_REQUEST = 5
MSG_DISCONNECT_RESPONSE = 6
MSG_PING_REQUEST = 7
MSG_PING_RESPONSE = 8
MSG_DEVICE_INFO_REQUEST = 9
MSG_DEVICE_INFO_RESPONSE = 10
MSG_LIST_ENTITIES_REQUEST = 11
MSG_LIST_ENTITIES_BINARY_SENSOR = 12
MSG_LIST_ENTITIES_SENSOR = 16
MSG_LIST_ENTITIES_TEXT_SENSOR = 18
MSG_LIST_ENTITIES_DONE = 19
MSG_SUBSCRIBE_STATES_REQUEST = 20
MSG_BINARY_SENSOR_STATE = 21
MSG_SENSOR_STATE = 25
MSG_TEXT_SENSOR_STATE = 27
MSG_SUBSCRIBE_LOGS_REQUEST = 28
MSG_SUBSCRIBE_HA_SERVICES_REQUEST = 34
MSG_GET_TIME_REQUEST = 36
MSG_GET_TIME_RESPONSE = 37
MSG_SUBSCRIBE_HA_STATES_REQUEST = 38
MSG_LIST_ENTITIES_NUMBER = 49
MSG_NUMBER_STATE = 50
MSG_NUMBER_COMMAND_REQUEST = 51
MSG_LIST_ENTITIES_SELECT = 52
MSG_SELECT_STATE = 53
MSG_SELECT_COMMAND_REQUEST = 54
MSG_LIST_ENTITIES_BUTTON = 61
MSG_BUTTON_COMMAND_REQUEST = 62

_LIST_MESSAGE: dict[Kind, tuple[int, type]] = {
    Kind.SENSOR: (MSG_LIST_ENTITIES_SENSOR, pb.ListEntitiesSensorResponse),
    Kind.BINARY_SENSOR: (MSG_LIST_ENTITIES_BINARY_SENSOR, pb.ListEntitiesBinarySensorResponse),
    Kind.TEXT_SENSOR: (MSG_LIST_ENTITIES_TEXT_SENSOR, pb.ListEntitiesTextSensorResponse),
    Kind.SELECT: (MSG_LIST_ENTITIES_SELECT, pb.ListEntitiesSelectResponse),
    Kind.NUMBER: (MSG_LIST_ENTITIES_NUMBER, pb.ListEntitiesNumberResponse),
    Kind.BUTTON: (MSG_LIST_ENTITIES_BUTTON, pb.ListEntitiesButtonResponse),
}

_STATE_MESSAGE: dict[Kind, tuple[int, type]] = {
    Kind.SENSOR: (MSG_SENSOR_STATE, pb.SensorStateResponse),
    Kind.BINARY_SENSOR: (MSG_BINARY_SENSOR_STATE, pb.BinarySensorStateResponse),
    Kind.TEXT_SENSOR: (MSG_TEXT_SENSOR_STATE, pb.TextSensorStateResponse),
    Kind.SELECT: (MSG_SELECT_STATE, pb.SelectStateResponse),
    Kind.NUMBER: (MSG_NUMBER_STATE, pb.NumberStateResponse),
}


def local_mac_address(adapter: str | None = None) -> str:
    """A stable MAC for this device, which Home Assistant keys the device on.

    The Bluetooth adapter's own address is the honest choice: the probes are
    reached through it, and it is stable across restarts.  If it cannot be
    read, fall back to a deterministic locally-administered address derived
    from the hostname, which is stable for as long as the hostname is.
    """
    candidates = [adapter] if adapter else []
    bluetooth_root = Path("/sys/class/bluetooth")
    if bluetooth_root.is_dir():
        candidates += sorted(p.name for p in bluetooth_root.iterdir())
    for name in candidates:
        address_file = bluetooth_root / name / "address"
        try:
            value = address_file.read_text().strip().upper()
        except OSError:
            continue
        if len(value) == 17:
            return value

    digest = abs(hash(socket.gethostname())) & 0xFFFFFFFF
    octets = [0x02, 0x4D, 0x45]  # locally administered, then "ME"
    octets += [(digest >> shift) & 0xFF for shift in (16, 8, 0)]
    return ":".join(f"{o:02X}" for o in octets)


class _Connection:
    """One connected client."""

    def __init__(self, server: ESPHomeServer, writer: asyncio.StreamWriter, peer: str) -> None:
        self._server = server
        self._writer = writer
        self.peer = peer
        self.framer: PlaintextFramer | NoiseFramer | None = None
        self.hello_done = False
        self.subscribed = False
        self.client_info = "unknown"
        self._sent: dict[int, object] = {}
        self._closed = False

    # -- framing ---------------------------------------------------------

    def _select_framer(self, first_byte: int) -> None:
        if self._server.encryption_key is not None:
            if first_byte == 0x00:
                raise RequiresEncryption(
                    "client connected in plaintext, but an encryption key is configured"
                )
            self.framer = NoiseFramer(
                self._server.encryption_key,
                self._server.device_name,
                self._server.mac_address,
            )
        else:
            if first_byte == 0x01:
                raise RequiresEncryption(
                    "client requested encryption, but no encryption key is configured"
                )
            self.framer = PlaintextFramer()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        if self.framer is None:
            if not data:
                return []
            self._select_framer(data[0])
        assert self.framer is not None
        try:
            return self.framer.feed(data)
        finally:
            # Flush even on the way out of a failure: a rejected handshake
            # queues an explanatory frame, and without it the client sees a
            # bare EOF and cannot tell a wrong key from a dead network.
            self._flush_handshake()

    def _flush_handshake(self) -> None:
        if self.framer is None:
            return
        pending = self.framer.take_outbound()
        if pending:
            self._writer.write(pending)

    # -- sending ---------------------------------------------------------

    def send(self, msg_type: int, message) -> None:
        """Queue one protobuf message."""
        if self._closed or self.framer is None or not self.framer.ready:
            return
        try:
            self._writer.write(self.framer.encode(msg_type, message.SerializeToString()))
        except FrameError as exc:
            _LOGGER.debug("%s: %s", self.peer, exc)
            self.close()

    async def drain(self) -> None:
        if self._closed:
            return
        with contextlib.suppress(ConnectionError, OSError):
            await self._writer.drain()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(ConnectionError, OSError):
            self._writer.close()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- state de-duplication -------------------------------------------

    def state_changed(self, key: int, value: object) -> bool:
        """Whether this value differs from the last one sent on this connection."""
        if key in self._sent and self._sent[key] == value:
            return False
        self._sent[key] = value
        return True

    def forget_states(self) -> None:
        self._sent.clear()


class ESPHomeServer:
    """Serves the native API and advertises the device over mDNS."""

    def __init__(
        self,
        config: Config,
        entities: list[Entity],
        commands: SlotCommands,
        slot_lookup: Callable[[int], ProbeSlot | None],
        mac_address: str | None = None,
    ) -> None:
        self._config = config
        self._entities = entities
        self._commands = commands
        self._slot_lookup = slot_lookup
        self.device_name = config.esphome_name
        self.friendly_name = config.esphome_friendly_name
        self.mac_address = mac_address or local_mac_address(config.adapter)
        self.encryption_key: bytes | None = (
            decode_noise_psk(config.esphome_encryption_key)
            if config.esphome_encryption_key
            else None
        )
        self._connections: set[_Connection] = set()
        self._server: asyncio.Server | None = None
        self._zeroconf: Zeroconf | None = None
        self._service_info: AsyncServiceInfo | None = None
        self._boot_time = time.time()

    @property
    def port(self) -> int:
        return self._config.esphome_port

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._config.esphome_host,
            port=self._config.esphome_port,
            reuse_address=True,
        )
        _LOGGER.info(
            "ESPHome native API on %s:%d as '%s' (%s, mac %s)",
            self._config.esphome_host,
            self._config.esphome_port,
            self.device_name,
            "encrypted" if self.encryption_key else "plaintext",
            self.mac_address,
        )
        if self._config.esphome_mdns:
            await self._register_mdns()

    async def stop(self) -> None:
        await self._unregister_mdns()
        for connection in list(self._connections):
            connection.close()
        self._connections.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _register_mdns(self) -> None:
        """Advertise ``_esphomelib._tcp`` so Home Assistant finds the device."""
        try:
            from zeroconf import IPVersion
            from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
        except ImportError:  # pragma: no cover
            _LOGGER.warning("zeroconf is unavailable; skipping mDNS advertisement")
            return

        properties = {
            "friendly_name": self.friendly_name,
            "mac": self.mac_address.replace(":", "").lower(),
            "version": f"meater-server {__version__}",
            "platform": "HOST",
            "board": "meater-server",
            "network": "ethernet",
        }
        if self.encryption_key is not None:
            properties["api_encryption"] = "Noise_NNpsk0_25519_ChaChaPoly_SHA256"

        self._aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        self._service_info = AsyncServiceInfo(
            "_esphomelib._tcp.local.",
            f"{self.device_name}._esphomelib._tcp.local.",
            port=self._config.esphome_port,
            properties=properties,
            server=f"{self.device_name}.local.",
        )
        try:
            await self._aiozc.async_register_service(self._service_info)
            _LOGGER.info("advertised %s._esphomelib._tcp.local.", self.device_name)
        except Exception as exc:
            _LOGGER.warning("mDNS advertisement failed: %s", exc)
            self._service_info = None

    async def _unregister_mdns(self) -> None:
        aiozc = getattr(self, "_aiozc", None)
        if aiozc is None:
            return
        with contextlib.suppress(Exception):
            if self._service_info is not None:
                await aiozc.async_unregister_service(self._service_info)
            await aiozc.async_close()
        self._aiozc = None
        self._service_info = None

    # -- connections -----------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else "?"
        connection = _Connection(self, writer, peer)
        self._connections.add(connection)
        _LOGGER.debug("%s: connected", peer)
        try:
            while not connection.closed:
                data = await reader.read(8192)
                if not data:
                    break
                for msg_type, payload in connection.feed(data):
                    self._dispatch(connection, msg_type, payload)
                await connection.drain()
        except RequiresEncryption as exc:
            _LOGGER.warning("%s: %s", peer, exc)
        except (FrameError, ValueError) as exc:
            _LOGGER.warning("%s: protocol error: %s", peer, exc)
            # Let any rejection frame reach the client before the socket goes.
            await connection.drain()
        except (ConnectionError, OSError) as exc:
            _LOGGER.debug("%s: %s", peer, exc)
        except asyncio.CancelledError:
            raise
        finally:
            self._connections.discard(connection)
            connection.close()
            _LOGGER.debug("%s: disconnected", peer)

    def _dispatch(self, connection: _Connection, msg_type: int, payload: bytes) -> None:
        handler = _HANDLERS.get(msg_type)
        if handler is None:
            _LOGGER.debug("%s: ignoring message type %d", connection.peer, msg_type)
            return
        handler(self, connection, payload)

    # -- handlers --------------------------------------------------------

    def _on_hello(self, connection: _Connection, payload: bytes) -> None:
        request = pb.HelloRequest()
        request.ParseFromString(payload)
        connection.client_info = request.client_info or "unknown"
        connection.hello_done = True
        _LOGGER.info("%s: hello from %s", connection.peer, connection.client_info)
        response = pb.HelloResponse(
            api_version_major=API_VERSION_MAJOR,
            api_version_minor=API_VERSION_MINOR,
            server_info=f"meater-server {__version__}",
            name=self.device_name,
        )
        connection.send(MSG_HELLO_RESPONSE, response)

    def _on_legacy_connect(self, connection: _Connection, _payload: bytes) -> None:
        """Older clients send a ConnectRequest; an empty response means success.

        The message was removed from api.proto when API password auth went
        away, so there is no generated class to build - but the two-byte
        empty-response frame is all an older client needs.
        """
        if connection.framer is None or not connection.framer.ready:
            return
        connection._writer.write(connection.framer.encode(MSG_LEGACY_CONNECT_RESPONSE, b""))

    def _on_disconnect_request(self, connection: _Connection, _payload: bytes) -> None:
        connection.send(MSG_DISCONNECT_RESPONSE, pb.DisconnectResponse())
        connection.close()

    def _on_disconnect_response(self, connection: _Connection, _payload: bytes) -> None:
        connection.close()

    def _on_ping(self, connection: _Connection, _payload: bytes) -> None:
        connection.send(MSG_PING_RESPONSE, pb.PingResponse())

    def _on_device_info(self, connection: _Connection, _payload: bytes) -> None:
        response = pb.DeviceInfoResponse(
            uses_password=False,
            name=self.device_name,
            friendly_name=self.friendly_name,
            mac_address=self.mac_address,
            esphome_version=f"meater-server {__version__}",
            compilation_time=time.strftime(
                "%b %d %Y, %H:%M:%S", time.localtime(self._boot_time)
            ),
            model="MEATER probe bridge",
            manufacturer="meater-server",
            project_name="meater-server",
            project_version=__version__,
            has_deep_sleep=False,
            webserver_port=self._config.web_port if self._config.web_enabled else 0,
            api_encryption_supported=self.encryption_key is not None,
            suggested_area="Kitchen",
        )
        connection.send(MSG_DEVICE_INFO_RESPONSE, response)

    def _on_list_entities(self, connection: _Connection, _payload: bytes) -> None:
        for entity in self._entities:
            msg_type, message_cls = _LIST_MESSAGE[entity.kind]
            connection.send(msg_type, _build_list_message(message_cls, entity))
        connection.send(MSG_LIST_ENTITIES_DONE, pb.ListEntitiesDoneResponse())

    def _on_subscribe_states(self, connection: _Connection, _payload: bytes) -> None:
        connection.subscribed = True
        connection.forget_states()
        self._send_states(connection, self._entities)

    def _on_get_time(self, connection: _Connection, _payload: bytes) -> None:
        connection.send(
            MSG_GET_TIME_RESPONSE, pb.GetTimeResponse(epoch_seconds=int(time.time()))
        )

    def _on_ignored(self, _connection: _Connection, _payload: bytes) -> None:
        """Subscriptions this device has nothing to publish on."""

    def _on_select_command(self, connection: _Connection, payload: bytes) -> None:
        request = pb.SelectCommandRequest()
        request.ParseFromString(payload)
        self._apply_command(connection, request.key, request.state)

    def _on_number_command(self, connection: _Connection, payload: bytes) -> None:
        request = pb.NumberCommandRequest()
        request.ParseFromString(payload)
        self._apply_command(connection, request.key, request.state)

    def _on_button_command(self, connection: _Connection, payload: bytes) -> None:
        request = pb.ButtonCommandRequest()
        request.ParseFromString(payload)
        self._apply_command(connection, request.key, None)

    def _apply_command(self, connection: _Connection, key: int, value: object) -> None:
        entity = next((e for e in self._entities if e.key == key), None)
        if entity is None or entity.command_fn is None:
            _LOGGER.debug("%s: command for unknown entity key %d", connection.peer, key)
            return
        slot = self._slot_lookup(entity.slot_index)
        if slot is None:
            _LOGGER.debug("%s: command for missing slot %d", connection.peer, entity.slot_index)
            return
        _LOGGER.info("%s set %s to %r", connection.client_info, entity.object_id, value)
        try:
            entity.command_fn(slot, value)
        except Exception:
            _LOGGER.exception("command on %s failed", entity.object_id)
            return
        self.publish_states()

    # -- state publication -----------------------------------------------

    def publish_states(self, slot_index: int | None = None) -> None:
        """Push changed states to every subscribed client."""
        entities = (
            self._entities
            if slot_index is None
            else [e for e in self._entities if e.slot_index == slot_index]
        )
        for connection in list(self._connections):
            if connection.subscribed and not connection.closed:
                self._send_states(connection, entities)

    def _send_states(self, connection: _Connection, entities: Iterable[Entity]) -> None:
        for entity in entities:
            state_message = _STATE_MESSAGE.get(entity.kind)
            if state_message is None:
                continue  # buttons are write-only
            slot = self._slot_lookup(entity.slot_index)
            if slot is None:
                continue
            value = entity.read(slot)
            if not connection.state_changed(entity.key, value):
                continue
            msg_type, message_cls = state_message
            connection.send(msg_type, _build_state_message(message_cls, entity, value))


def _build_list_message(message_cls: type, entity: Entity):
    """Fill the ListEntities*Response fields this entity kind actually has."""
    message = message_cls(
        object_id=entity.object_id,
        key=entity.key,
        name=entity.name,
        disabled_by_default=False,
        entity_category=entity.entity_category,
    )
    fields = {f.name for f in message_cls.DESCRIPTOR.fields}
    if entity.icon and "icon" in fields:
        message.icon = entity.icon
    if entity.unit and "unit_of_measurement" in fields:
        message.unit_of_measurement = entity.unit
    if entity.device_class and "device_class" in fields:
        message.device_class = entity.device_class
    if "state_class" in fields:
        message.state_class = entity.state_class
    if "accuracy_decimals" in fields:
        message.accuracy_decimals = entity.accuracy_decimals
    if entity.options and "options" in fields:
        message.options.extend(entity.options)
    if "min_value" in fields:
        message.min_value = entity.min_value
        message.max_value = entity.max_value
        message.step = entity.step
    if "mode" in fields:
        message.mode = entity.mode
    return message


def _build_state_message(message_cls: type, entity: Entity, value: object):
    """Build a *StateResponse, marking the state missing when there is none."""
    if value is None:
        return message_cls(key=entity.key, missing_state=True)
    if entity.kind is Kind.BINARY_SENSOR:
        return message_cls(key=entity.key, state=bool(value))
    if entity.kind in (Kind.TEXT_SENSOR, Kind.SELECT):
        return message_cls(key=entity.key, state=str(value))
    return message_cls(key=entity.key, state=float(value))


_HANDLERS: dict[int, Callable[[ESPHomeServer, _Connection, bytes], None]] = {
    MSG_HELLO_REQUEST: ESPHomeServer._on_hello,
    MSG_LEGACY_CONNECT_REQUEST: ESPHomeServer._on_legacy_connect,
    MSG_DISCONNECT_REQUEST: ESPHomeServer._on_disconnect_request,
    MSG_DISCONNECT_RESPONSE: ESPHomeServer._on_disconnect_response,
    MSG_PING_REQUEST: ESPHomeServer._on_ping,
    MSG_DEVICE_INFO_REQUEST: ESPHomeServer._on_device_info,
    MSG_LIST_ENTITIES_REQUEST: ESPHomeServer._on_list_entities,
    MSG_SUBSCRIBE_STATES_REQUEST: ESPHomeServer._on_subscribe_states,
    MSG_SUBSCRIBE_LOGS_REQUEST: ESPHomeServer._on_ignored,
    MSG_SUBSCRIBE_HA_SERVICES_REQUEST: ESPHomeServer._on_ignored,
    MSG_SUBSCRIBE_HA_STATES_REQUEST: ESPHomeServer._on_ignored,
    MSG_GET_TIME_REQUEST: ESPHomeServer._on_get_time,
    MSG_SELECT_COMMAND_REQUEST: ESPHomeServer._on_select_command,
    MSG_NUMBER_COMMAND_REQUEST: ESPHomeServer._on_number_command,
    MSG_BUTTON_COMMAND_REQUEST: ESPHomeServer._on_button_command,
}
