"""Wire framing for the ESPHome native API, from the device's side.

The protocol has two framings, distinguished by the first byte of the stream:

``0x00``
    Plaintext.  Each frame is ``0x00``, a varuint length, a varuint message
    type, then that many bytes of protobuf.

``0x01``
    Noise, pattern ``Noise_NNpsk0_25519_ChaChaPoly_SHA256`` with the prologue
    ``NoiseAPIInit\\x00\\x00`` and the shared key as the pre-shared key.  Each
    frame is ``0x01`` and a 16-bit big-endian length, then the body.  Before
    the handshake completes the body is handshake material; afterwards it is
    ciphertext over ``type:u16be | length:u16be | protobuf``.

The client opens a Noise connection by sending an empty frame followed
immediately by its handshake message, so both usually arrive in one read.
"""

from __future__ import annotations

import binascii
from enum import Enum, auto

from noise.connection import NoiseConnection

NOISE_PROTOCOL_NAME = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
NOISE_PROLOGUE = b"NoiseAPIInit\x00\x00"

#: The only handshake the protocol defines, echoed back in the ServerHello.
CHOSEN_PROTO = 0x01
#: Frame body prefix meaning "no error".
_OK = b"\x00"
#: Frame body prefix meaning "handshake rejected"; the rest is a reason string.
_ERR = b"\x01"
_MAC_FAILURE = b"Handshake MAC failure"

MAX_FRAME_SIZE = 65535
_MAX_VARUINT_BYTES = 4


class FrameError(Exception):
    """The peer sent something that is not valid framing."""


class RequiresEncryption(FrameError):
    """A plaintext client reached an encrypted endpoint, or the reverse."""


def decode_noise_psk(psk: str) -> bytes:
    """Decode a base64 encryption key into its raw 32 bytes."""
    try:
        raw = binascii.a2b_base64(psk)
    except binascii.Error as exc:
        raise ValueError(f"encryption key is not valid base64: {exc}") from exc
    if len(raw) != 32:
        raise ValueError(
            f"encryption key decodes to {len(raw)} bytes, expected 32"
        )
    return raw


def _varuint(value: int) -> bytes:
    if value <= 0x7F:
        return bytes((value,))
    out = bytearray()
    while value:
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | 0x80 if value else chunk)
    return bytes(out)


class PlaintextFramer:
    """Unencrypted framing."""

    requires_handshake = False

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._outbound = bytearray()
        self.ready = True

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buffer += data
        messages: list[tuple[int, bytes]] = []
        while True:
            parsed = self._parse_one()
            if parsed is None:
                return messages
            messages.append(parsed)

    def _parse_one(self) -> tuple[int, bytes] | None:
        buffer = self._buffer
        if len(buffer) < 3:
            return None
        if buffer[0] != 0x00:
            if buffer[0] == 0x01:
                raise RequiresEncryption(
                    "client opened an encrypted connection, but this endpoint "
                    "has no encryption key configured"
                )
            raise FrameError(f"invalid preamble 0x{buffer[0]:02x}")

        pos = 1
        length, pos = self._read_varuint(buffer, pos)
        if length is None:
            return None
        if length > MAX_FRAME_SIZE:
            raise FrameError(f"frame length {length} exceeds {MAX_FRAME_SIZE}")
        msg_type, pos = self._read_varuint(buffer, pos)
        if msg_type is None:
            return None
        if len(buffer) < pos + length:
            return None

        payload = bytes(buffer[pos : pos + length])
        del buffer[: pos + length]
        return msg_type, payload

    @staticmethod
    def _read_varuint(buffer: bytearray, pos: int) -> tuple[int | None, int]:
        result = 0
        bitpos = 0
        start = pos
        while pos < len(buffer):
            byte = buffer[pos]
            pos += 1
            result |= (byte & 0x7F) << bitpos
            if not byte & 0x80:
                return result, pos
            bitpos += 7
            if pos - start >= _MAX_VARUINT_BYTES:
                raise FrameError("varuint too long")
        return None, pos

    def encode(self, msg_type: int, payload: bytes) -> bytes:
        return b"".join((b"\x00", _varuint(len(payload)), _varuint(msg_type), payload))

    def take_outbound(self) -> bytes:
        return b""


class _NoiseState(Enum):
    HELLO = auto()
    HANDSHAKE = auto()
    READY = auto()
    FAILED = auto()


class NoiseFramer:
    """Encrypted framing, acting as the Noise responder."""

    requires_handshake = True

    def __init__(self, psk: bytes, device_name: str, mac_address: str) -> None:
        self._psk = psk
        self._device_name = device_name
        self._mac_address = mac_address
        self._buffer = bytearray()
        self._outbound = bytearray()
        self._state = _NoiseState.HELLO
        self._proto = NoiseConnection.from_name(NOISE_PROTOCOL_NAME)
        self._proto.set_as_responder()
        self._proto.set_psks(psk)
        self._proto.set_prologue(NOISE_PROLOGUE)
        self._proto.start_handshake()

    @property
    def ready(self) -> bool:
        return self._state is _NoiseState.READY

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buffer += data
        messages: list[tuple[int, bytes]] = []
        while len(self._buffer) >= 3:
            if self._buffer[0] != CHOSEN_PROTO:
                if self._buffer[0] == 0x00:
                    raise RequiresEncryption(
                        "client opened a plaintext connection, but this "
                        "endpoint requires encryption"
                    )
                raise FrameError(f"invalid preamble 0x{self._buffer[0]:02x}")
            length = (self._buffer[1] << 8) | self._buffer[2]
            if len(self._buffer) < 3 + length:
                break
            frame = bytes(self._buffer[3 : 3 + length])
            del self._buffer[: 3 + length]

            if self._state is _NoiseState.HELLO:
                self._handle_client_hello()
            elif self._state is _NoiseState.HANDSHAKE:
                self._handle_handshake(frame)
            elif self._state is _NoiseState.READY:
                messages.append(self._decrypt(frame))
            else:
                raise FrameError("frame received after handshake failure")
        return messages

    def _handle_client_hello(self) -> None:
        """Answer the empty ClientHello with our chosen proto, name and MAC."""
        body = (
            bytes((CHOSEN_PROTO,))
            + self._device_name.encode("utf-8")
            + b"\x00"
            + self._mac_address.encode("utf-8")
            + b"\x00"
        )
        self._outbound += _noise_frame(body)
        self._state = _NoiseState.HANDSHAKE

    def _handle_handshake(self, frame: bytes) -> None:
        if not frame or frame[0] != 0x00:
            self._fail()
            raise FrameError("client handshake frame carried an error prefix")
        try:
            self._proto.read_message(frame[1:])
            response = self._proto.write_message()
        except Exception as exc:
            # A wrong key shows up here as an AEAD tag failure.  ESPHome
            # answers with this exact string, which the client turns into
            # "Invalid encryption key".
            self._fail()
            raise FrameError(f"handshake failed: {exc}") from exc
        self._outbound += _noise_frame(_OK + response)
        self._state = _NoiseState.READY

    def _fail(self) -> None:
        self._state = _NoiseState.FAILED
        self._outbound += _noise_frame(_ERR + _MAC_FAILURE)

    def _decrypt(self, frame: bytes) -> tuple[int, bytes]:
        try:
            msg = self._proto.decrypt(frame)
        except Exception as exc:
            raise FrameError(f"decryption failed: {exc}") from exc
        if len(msg) < 4:
            raise FrameError(f"decrypted frame too short: {len(msg)} bytes")
        msg_type = (msg[0] << 8) | msg[1]
        return msg_type, bytes(msg[4:])

    def encode(self, msg_type: int, payload: bytes) -> bytes:
        if not self.ready:
            raise FrameError("cannot encode before the handshake completes")
        body = (
            bytes(((msg_type >> 8) & 0xFF, msg_type & 0xFF))
            + bytes(((len(payload) >> 8) & 0xFF, len(payload) & 0xFF))
            + payload
        )
        return _noise_frame(self._proto.encrypt(body))

    def take_outbound(self) -> bytes:
        out = bytes(self._outbound)
        self._outbound.clear()
        return out


def _noise_frame(body: bytes) -> bytes:
    if len(body) > MAX_FRAME_SIZE:
        raise FrameError(f"frame body {len(body)} exceeds {MAX_FRAME_SIZE}")
    return bytes((CHOSEN_PROTO, (len(body) >> 8) & 0xFF, len(body) & 0xFF)) + body
