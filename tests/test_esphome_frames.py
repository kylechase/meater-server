"""Loopback the server framing against aioesphomeapi's real client framing.

aioesphomeapi is what Home Assistant actually uses to talk to ESPHome
devices, so driving its frame helpers directly is the closest thing to a
conformance test available without a Home Assistant instance.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from aioesphomeapi._frame_helper.noise import APINoiseFrameHelper
from aioesphomeapi._frame_helper.plain_text import APIPlaintextFrameHelper
from aioesphomeapi.connection import APIConnection, ConnectionParams
from aioesphomeapi.zeroconf import ZeroconfManager

from meater_server.esphome.frames import (
    FrameError,
    NoiseFramer,
    PlaintextFramer,
    RequiresEncryption,
    decode_noise_psk,
)

PSK = base64.b64encode(bytes(range(32))).decode()
DEVICE_NAME = "meater"
DEVICE_MAC = "AA:BB:CC:DD:EE:FF"


class FakeTransport(asyncio.Transport):
    """Collects everything the client writes."""

    def __init__(self) -> None:
        super().__init__()
        self.sent = bytearray()

    def writelines(self, chunks) -> None:
        for chunk in chunks:
            self.sent += chunk

    def write(self, data) -> None:
        self.sent += data

    def close(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def drain(self) -> None:
        pass


class RecordingConnection(APIConnection):
    """A real APIConnection that records what the frame helper hands up.

    The frame helpers are Cython classes that type-check this argument, so a
    stand-in object will not do - and using the real one means the test
    exercises the same code path Home Assistant does.
    """

    def __init__(
        self,
        noise_psk: str | None,
        expected_name: str | None,
        expected_mac: str | None = None,
    ) -> None:
        params = ConnectionParams(
            addresses=["127.0.0.1"],
            port=6053,
            password=None,
            client_info="test",
            keepalive=15.0,
            zeroconf_manager=ZeroconfManager(),
            noise_psk=noise_psk,
            expected_name=expected_name,
            expected_mac=expected_mac,
        )
        super().__init__(params, None, False, "test")
        self.received: list[tuple[int, bytes]] = []
        self.errors: list[Exception] = []

    def process_packet(self, msg_type: int, payload: bytes) -> None:
        self.received.append((msg_type, bytes(payload)))

    def report_fatal_error(self, exc: Exception) -> None:
        self.errors.append(exc)


def _take(transport: FakeTransport) -> bytes:
    out = bytes(transport.sent)
    transport.sent.clear()
    return out


class Peer:
    """A client frame helper wired to a fake transport."""

    def __init__(self, helper, connection: RecordingConnection, transport: FakeTransport):
        self.helper = helper
        self.connection = connection
        self.transport = transport

    def take(self) -> bytes:
        return _take(self.transport)

    def send(self, msg_type: int, payload: bytes) -> bytes:
        self.helper.write_packets([(msg_type, payload)], False)
        return self.take()


def _plaintext_peer() -> Peer:
    connection = RecordingConnection(None, None)
    transport = FakeTransport()
    helper = APIPlaintextFrameHelper(
        connection=connection, client_info="test", log_name="test"
    )
    helper.connection_made(transport)
    return Peer(helper, connection, transport)


def _noise_peer(
    psk: str = PSK,
    expected_name: str | None = DEVICE_NAME,
    expected_mac: str | None = None,
) -> Peer:
    connection = RecordingConnection(psk, expected_name, expected_mac)
    transport = FakeTransport()
    helper = APINoiseFrameHelper(
        connection=connection,
        noise_psk=psk,
        expected_name=expected_name,
        expected_mac=expected_mac,
        client_info="test",
        log_name="test",
    )
    helper.connection_made(transport)
    return Peer(helper, connection, transport)


# --- plaintext ------------------------------------------------------------


async def test_plaintext_roundtrip_both_directions():
    client = _plaintext_peer()
    server = PlaintextFramer()

    # Client -> server, including an empty payload and a multi-byte varuint type.
    wire = client.send(1, b"\x0a\x04test")
    wire += client.send(7, b"")
    wire += client.send(200, b"x" * 300)
    assert server.feed(wire) == [
        (1, b"\x0a\x04test"),
        (7, b""),
        (200, b"x" * 300),
    ]

    # Server -> client.
    client.helper.data_received(server.encode(2, b"\x18\x01"))
    client.helper.data_received(server.encode(25, b""))
    assert client.connection.received == [(2, b"\x18\x01"), (25, b"")]


async def test_plaintext_tolerates_arbitrary_fragmentation():
    client = _plaintext_peer()
    server = PlaintextFramer()
    wire = client.send(16, b"hello world" * 40)

    got: list[tuple[int, bytes]] = []
    for i in range(0, len(wire), 7):
        got.extend(server.feed(wire[i : i + 7]))
    assert got == [(16, b"hello world" * 40)]


async def test_plaintext_rejects_an_encrypted_client():
    server = PlaintextFramer()
    with pytest.raises(RequiresEncryption):
        server.feed(b"\x01\x00\x00")


def test_plaintext_rejects_a_bad_preamble():
    server = PlaintextFramer()
    with pytest.raises(FrameError):
        server.feed(b"\x42\x00\x00")


# --- noise ----------------------------------------------------------------


def _complete_handshake(client: Peer, server: NoiseFramer) -> None:
    """Drive the handshake to completion in both directions."""
    # The client writes its hello and handshake together on connect.
    assert server.feed(client.take()) == []
    client.helper.data_received(server.take_outbound())
    assert server.ready
    assert client.helper.ready_future.done()
    client.helper.ready_future.result()


async def test_noise_handshake_and_roundtrip():
    client = _noise_peer()
    server = NoiseFramer(decode_noise_psk(PSK), DEVICE_NAME, DEVICE_MAC)
    _complete_handshake(client, server)

    # Client -> server over the established cipher.
    assert server.feed(client.send(9, b"")) == [(9, b"")]
    assert server.feed(client.send(11, b"\x08\x01")) == [(11, b"\x08\x01")]

    # Server -> client.
    client.helper.data_received(server.encode(10, b"\x12\x06meater"))
    client.helper.data_received(server.encode(25, b"\x15\x00\x00\x80?"))
    assert client.connection.received == [
        (10, b"\x12\x06meater"),
        (25, b"\x15\x00\x00\x80?"),
    ]
    assert client.connection.errors == []


async def test_noise_server_hello_carries_name_and_mac():
    """The client validates both labels out of the ServerHello body."""
    # A client told to expect this exact name and MAC completes the handshake,
    # which it can only do if both were parsed out of our ServerHello.
    client = _noise_peer(expected_name=DEVICE_NAME, expected_mac=DEVICE_MAC)
    server = NoiseFramer(decode_noise_psk(PSK), DEVICE_NAME, DEVICE_MAC)
    _complete_handshake(client, server)
    assert client.connection.errors == []


async def test_noise_client_rejects_a_mismatched_mac():
    client = _noise_peer(expected_mac="00:11:22:33:44:55")
    server = NoiseFramer(decode_noise_psk(PSK), DEVICE_NAME, DEVICE_MAC)
    server.feed(client.take())
    client.helper.data_received(server.take_outbound())
    assert client.connection.errors, "client should reject the wrong MAC"


async def test_noise_client_rejects_a_mismatched_device_name():
    client = _noise_peer(expected_name="something-else")
    server = NoiseFramer(decode_noise_psk(PSK), DEVICE_NAME, DEVICE_MAC)
    server.feed(client.take())
    client.helper.data_received(server.take_outbound())
    assert client.connection.errors, "client should reject the wrong device name"


async def test_noise_wrong_key_fails_the_handshake():
    other = base64.b64encode(bytes(range(32, 64))).decode()
    client = _noise_peer(psk=other)
    server = NoiseFramer(decode_noise_psk(PSK), DEVICE_NAME, DEVICE_MAC)
    with pytest.raises(FrameError):
        server.feed(client.take())
    # The server still reports the failure the way the client expects, so it
    # can say "invalid encryption key" rather than something generic.
    client.helper.data_received(server.take_outbound())
    assert client.connection.errors
    assert "encryption key" in str(client.connection.errors[0]).lower()


async def test_noise_rejects_a_plaintext_client():
    server = NoiseFramer(decode_noise_psk(PSK), DEVICE_NAME, DEVICE_MAC)
    with pytest.raises(RequiresEncryption):
        server.feed(b"\x00\x00\x01")


async def test_noise_tolerates_fragmentation():
    client = _noise_peer()
    server = NoiseFramer(decode_noise_psk(PSK), DEVICE_NAME, DEVICE_MAC)
    handshake = client.take()
    for i in range(0, len(handshake), 3):
        server.feed(handshake[i : i + 3])
    client.helper.data_received(server.take_outbound())
    assert server.ready

    wire = client.send(11, b"payload" * 50)
    got: list[tuple[int, bytes]] = []
    for i in range(0, len(wire), 5):
        got.extend(server.feed(wire[i : i + 5]))
    assert got == [(11, b"payload" * 50)]


# --- key handling ---------------------------------------------------------


def test_decode_noise_psk_round_trip():
    assert decode_noise_psk(PSK) == bytes(range(32))


@pytest.mark.parametrize("bad", ["", "short", base64.b64encode(b"x" * 16).decode()])
def test_decode_noise_psk_rejects_wrong_length(bad: str):
    with pytest.raises(ValueError):
        decode_noise_psk(bad)
