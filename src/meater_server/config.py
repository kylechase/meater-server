"""Configuration: defaults, a TOML file, and environment overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def _default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "meater-server" / "state.json"


def _config_search_paths() -> list[Path]:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return [
        Path("meater.toml"),
        Path(base) / "meater-server" / "config.toml",
        Path("/etc/meater-server/config.toml"),
    ]


@dataclass
class Config:
    """Everything the server needs to know, with working defaults."""

    # --- Bluetooth ---
    adapter: str | None = None
    """BlueZ adapter, e.g. ``hci0``.  ``None`` uses the default adapter."""
    addresses: list[str] = field(default_factory=list)
    """Probe MAC addresses to bind to slots, in slot order.  Empty = first found."""
    slots: int = 1
    """How many probes the server exposes.  Fixed at startup; see ProbeSlot."""
    scan_timeout: float = 12.0
    reconnect_delay: float = 5.0
    stale_after: float = 45.0
    """Mark a probe disconnected if no reading arrives within this many seconds."""

    # --- Display ---
    units: str = "C"
    """``C`` or ``F``, for the web UI.  ESPHome always reports Celsius."""

    # --- Web UI ---
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    # --- ESPHome native API ---
    esphome_enabled: bool = True
    esphome_host: str = "0.0.0.0"
    esphome_port: int = 6053
    esphome_name: str = "meater"
    """Device name.  Must be a valid hostname label; becomes ``<name>.local``."""
    esphome_friendly_name: str = "MEATER"
    esphome_encryption_key: str | None = None
    """Base64 32-byte Noise PSK.  ``None`` serves the plaintext API."""
    esphome_mdns: bool = True
    """Advertise ``_esphomelib._tcp.local.`` so Home Assistant auto-discovers."""

    # --- Alerts ---
    notify_ntfy_url: str | None = None
    """ntfy topic URL, e.g. ``https://ntfy.sh/my-grill``.  ``None`` sends nothing."""
    notify_ntfy_token: str | None = None
    """Access token, for a topic that needs one."""

    # --- Persistence ---
    state_file: Path = field(default_factory=_default_state_path)

    # --- Presets ---
    presets: list[dict] = field(default_factory=list)
    """Overrides and additions merged over the builtin preset table."""

    def __post_init__(self) -> None:
        self.units = "F" if str(self.units).upper().startswith("F") else "C"
        self.addresses = [a.strip().upper() for a in self.addresses if str(a).strip()]
        self.slots = max(1, min(8, int(self.slots or 1)))
        if self.addresses:
            self.slots = max(self.slots, len(self.addresses))
        self.state_file = Path(self.state_file).expanduser()

    @classmethod
    def load(cls, path: str | Path | None = None) -> tuple[Config, Path | None]:
        """Load config, returning it alongside the file it came from, if any."""
        candidates = [Path(path)] if path else _config_search_paths()
        raw: dict = {}
        source: Path | None = None
        for candidate in candidates:
            expanded = candidate.expanduser()
            if expanded.is_file():
                with expanded.open("rb") as handle:
                    raw = tomllib.load(handle)
                source = expanded
                break
        else:
            if path:
                raise FileNotFoundError(f"config file not found: {path}")

        return cls.from_mapping(raw), source

    @classmethod
    def from_mapping(cls, raw: dict) -> Config:
        """Build a Config from a nested TOML mapping, then apply env overrides."""
        kwargs: dict = {}
        flat = {
            **{k: v for k, v in raw.items() if not isinstance(v, dict | list)},
            **_prefix(raw.get("bluetooth"), ""),
            **_prefix(raw.get("web"), "web_"),
            **_prefix(raw.get("esphome"), "esphome_"),
            **_prefix(raw.get("notify"), "notify_"),
        }
        if isinstance(raw.get("bluetooth"), dict):
            addresses = raw["bluetooth"].get("addresses")
            if isinstance(addresses, list):
                flat["addresses"] = addresses
        if isinstance(raw.get("presets"), list):
            flat["presets"] = raw["presets"]
        if isinstance(raw.get("state_file"), str):
            flat["state_file"] = raw["state_file"]

        known = {f.name for f in fields(cls)}
        for key, value in flat.items():
            if key in known:
                kwargs[key] = value

        config = cls(**kwargs)
        config._apply_env()
        return config

    def _apply_env(self) -> None:
        """``MEATER_<FIELD>`` overrides any file value."""
        for f in fields(self):
            env_key = f"MEATER_{f.name.upper()}"
            if env_key not in os.environ:
                continue
            value = os.environ[env_key]
            if f.name == "addresses":
                self.addresses = [v for v in value.replace(",", " ").split() if v]
            elif f.name == "presets":
                continue
            elif f.name == "state_file":
                self.state_file = Path(value)
            elif f.type in ("bool",) or isinstance(getattr(self, f.name), bool):
                setattr(self, f.name, value.strip().lower() in ("1", "true", "yes", "on"))
            elif isinstance(getattr(self, f.name), int) and not isinstance(
                getattr(self, f.name), bool
            ):
                setattr(self, f.name, int(value))
            elif isinstance(getattr(self, f.name), float):
                setattr(self, f.name, float(value))
            else:
                setattr(self, f.name, value or None)
        self.__post_init__()


def _prefix(section: object, prefix: str) -> dict:
    if not isinstance(section, dict):
        return {}
    return {
        f"{prefix}{k}": v for k, v in section.items() if not isinstance(v, dict | list)
    }
