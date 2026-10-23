"""Configuration model and persistence. This module has no Qt dependency."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class ConfigError(ValueError):
    """Invalid or unsupported configuration."""


@dataclass(frozen=True)
class MidiConfig:
    device_name: str = ""
    mode: str = "note"  # note or cc
    note: int | None = None
    controller: int | None = None
    channel: int | None = None  # MIDI channels are stored as 1..16; None means any.

    def validate(self) -> None:
        if not isinstance(self.device_name, str):
            raise ConfigError("MIDI device name must be a string")
        if self.mode not in {"note", "cc"}:
            raise ConfigError("MIDI mode must be note or cc")
        if self.note is not None and (type(self.note) is not int or not 0 <= self.note <= 127):
            raise ConfigError("MIDI note must be an integer between 0 and 127")
        if self.controller is not None and (
            type(self.controller) is not int or not 0 <= self.controller <= 127
        ):
            raise ConfigError("MIDI CC controller must be an integer between 0 and 127")
        if self.channel is not None and (
            type(self.channel) is not int or not 1 <= self.channel <= 16
        ):
            raise ConfigError("MIDI channel must be an integer between 1 and 16")


@dataclass(frozen=True)
class BackendConfig:
    kind: str = "none"  # none, serial, rigctld, flrig
    serial_port: str = ""
    serial_line: str = "RTS"  # RTS or DTR
    serial_active_low: bool = False
    host: str = ""
    port: int | None = None
    url: str = ""
    timeout_seconds: float = 2.0

    def validate(self) -> None:
        if not isinstance(self.kind, str):
            raise ConfigError("backend kind must be a string")
        for field_name in ("serial_port", "serial_line", "host", "url"):
            if not isinstance(getattr(self, field_name), str):
                raise ConfigError(f"{field_name} must be a string")
        if type(self.serial_active_low) is not bool:
            raise ConfigError("serial_active_low must be a boolean")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise ConfigError("Backend timeout must be numeric")
        if self.kind not in {"none", "serial", "rigctld", "flrig"}:
            raise ConfigError(f"Unsupported PTT backend: {self.kind}")
        if self.kind == "serial" and not self.serial_port.strip():
            raise ConfigError("Serial backend requires a port")
        if self.kind == "serial" and self.serial_line not in {"RTS", "DTR"}:
            raise ConfigError("Serial PTT line must be RTS or DTR")
        if self.kind == "rigctld":
            if not self.host.strip() or self.port is None:
                raise ConfigError("rigctld backend requires host and port")
            if type(self.port) is not int or not 1 <= self.port <= 65535:
                raise ConfigError("rigctld port must be between 1 and 65535")
        if self.kind == "flrig":
            if not self.url.strip():
                raise ConfigError("flrig backend requires an XML-RPC URL")
            if not self.url.lower().startswith(("http://", "https://")):
                raise ConfigError("flrig URL must use HTTP or HTTPS")
        if not 0.1 <= self.timeout_seconds <= 30:
            raise ConfigError("Backend timeout must be between 0.1 and 30 seconds")


@dataclass(frozen=True)
class AppConfig:
    schema_version: int = SCHEMA_VERSION
    setup_complete: bool = False
    midi: MidiConfig = MidiConfig()
    backend: BackendConfig = BackendConfig()
    tot_seconds: int = 60

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ConfigError(f"Unsupported configuration version: {self.schema_version}")
        if type(self.setup_complete) is not bool:
            raise ConfigError("setup_complete must be a boolean")
        self.midi.validate()
        self.backend.validate()
        if type(self.tot_seconds) is not int or not 1 <= self.tot_seconds <= 3600:
            raise ConfigError("TOT must be between 1 and 3600 seconds")
        if self.setup_complete:
            if not self.midi.device_name.strip():
                raise ConfigError("Completed setup requires a MIDI device")
            if self.midi.mode == "note" and self.midi.note is None:
                raise ConfigError("Completed setup requires a MIDI note")
            if self.midi.mode == "cc" and self.midi.controller is None:
                raise ConfigError("Completed setup requires a MIDI CC controller")
            if self.backend.kind == "none":
                raise ConfigError("Completed setup requires a PTT backend")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AppConfig":
        try:
            midi_value = value.get("midi", {})
            backend_value = value.get("backend", {})
            if not isinstance(midi_value, dict) or not isinstance(backend_value, dict):
                raise ConfigError("MIDI and backend settings must be objects")
            config = cls(
                schema_version=value.get("schema_version", 0),
                setup_complete=value.get("setup_complete", False),
                midi=MidiConfig(**midi_value),
                backend=BackendConfig(**backend_value),
                tot_seconds=value.get("tot_seconds", 60),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"Malformed configuration: {exc}") from exc
        config.validate()
        return config


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "midi_ptt_controller" / "config.json"


class ConfigStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_config_path()

    def load(self, *, create: bool = True) -> AppConfig:
        if not self.path.exists():
            config = AppConfig()
            if create:
                self.save(config)
            return config
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Cannot read configuration {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("Configuration root must be a JSON object")
        return AppConfig.from_dict(raw)

    def save(self, config: AppConfig) -> None:
        config.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(config), indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
