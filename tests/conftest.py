from __future__ import annotations

import socket
from pathlib import Path

import pytest

from meater_server.config import Config


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A server config bound to loopback on throwaway ports."""
    return Config(
        slots=2,
        web_host="127.0.0.1",
        web_port=free_port(),
        esphome_host="127.0.0.1",
        esphome_port=free_port(),
        esphome_mdns=False,
        state_file=tmp_path / "state.json",
    )
