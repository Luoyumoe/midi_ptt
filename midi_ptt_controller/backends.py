"""PTT output drivers. No Qt dependency; hardware libraries load lazily."""

from __future__ import annotations

import socket
import xmlrpc.client
from abc import ABC, abstractmethod
from typing import Any, Callable

from .config import BackendConfig


class BackendError(RuntimeError):
    """PTT backend could not safely complete an operation."""


class PttBackend(ABC):
    @abstractmethod
    def connect(self) -> None:
        """Open and initialize backend in the released state."""

    @abstractmethod
    def set_ptt(self, enabled: bool) -> None:
        """Set PTT state; return only after command is accepted."""

    @abstractmethod
    def close(self) -> None:
        """Release resources. Caller must request PTT off first."""


class SerialBackend(PttBackend):
    def __init__(self, config: BackendConfig, serial_factory: Callable[..., Any] | None = None):
        self.config = config
        self._serial_factory = serial_factory
        self._serial: Any | None = None

    def connect(self) -> None:
        try:
            if self._serial is not None and getattr(self._serial, "is_open", True):
                self.set_ptt(False)
                return
            if self._serial_factory is None:
                import serial

                factory = serial.Serial
            else:
                factory = self._serial_factory
            # Set inactive levels before open, avoiding an intentional key pulse.
            port = factory()
            self._serial = port
            idle_level = self.config.serial_active_low
            port.rts = idle_level
            port.dtr = idle_level
            port.baudrate = 9600
            port.timeout = self.config.timeout_seconds
            port.write_timeout = self.config.timeout_seconds
            port.port = self.config.serial_port
            port.open()
            self.set_ptt(False)
        except Exception as exc:
            self.close()
            raise BackendError(f"Serial PTT connection failed: {exc}") from exc

    def set_ptt(self, enabled: bool) -> None:
        if self._serial is None or not getattr(self._serial, "is_open", True):
            raise BackendError("Serial PTT backend is not connected")
        level = (not self.config.serial_active_low) if enabled else self.config.serial_active_low
        try:
            setattr(self._serial, self.config.serial_line.lower(), level)
        except Exception as exc:
            raise BackendError(f"Serial PTT update failed: {exc}") from exc

    def close(self) -> None:
        port = self._serial
        if port is not None:
            try:
                self.set_ptt(False)
            except Exception:
                pass
        self._serial = None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass


class RigctldBackend(PttBackend):
    def __init__(self, config: BackendConfig, socket_factory: Callable[..., Any] | None = None):
        self.config = config
        self._socket_factory = socket_factory or socket.create_connection
        self._connected = False

    def _command(self, enabled: bool) -> None:
        try:
            with self._socket_factory(
                (self.config.host, self.config.port), self.config.timeout_seconds
            ) as connection:
                connection.settimeout(self.config.timeout_seconds)
                connection.sendall(f"T {1 if enabled else 0}\n".encode("ascii"))
                response = bytearray()
                while len(response) < 4096:
                    chunk = connection.recv(1)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if chunk == b"\n":
                        break
            line = response.decode("ascii", errors="replace").strip()
            if line != "RPRT 0":
                raise BackendError(f"rigctld rejected PTT command: {line or 'no response'}")
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"rigctld PTT command failed: {exc}") from exc

    def connect(self) -> None:
        self._command(False)
        self._connected = True

    def set_ptt(self, enabled: bool) -> None:
        if not self._connected:
            raise BackendError("rigctld backend is not connected")
        self._command(enabled)

    def close(self) -> None:
        self._connected = False


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host: str):
        import http.client

        cached_host, connection = self._connection
        if connection and cached_host == host:
            return connection
        if connection:
            connection.close()
        connection = http.client.HTTPConnection(host, timeout=self.timeout)
        self._connection = (host, connection)
        return connection


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host: str):
        import http.client

        cached_host, connection = self._connection
        if connection and cached_host == host:
            return connection
        if connection:
            connection.close()
        connection = http.client.HTTPSConnection(host, timeout=self.timeout, context=self.context)
        self._connection = (host, connection)
        return connection


class FlrigBackend(PttBackend):
    def __init__(self, config: BackendConfig):
        self.config = config
        self._proxy: Any | None = None
        self._connected = False

    def _new_proxy(self):
        transport: xmlrpc.client.Transport
        if self.config.url.lower().startswith("https://"):
            transport = _TimeoutSafeTransport(self.config.timeout_seconds)
        else:
            transport = _TimeoutTransport(self.config.timeout_seconds)
        return xmlrpc.client.ServerProxy(
            self.config.url, transport=transport, allow_none=True, use_builtin_types=True
        )

    def connect(self) -> None:
        try:
            self._proxy = self._new_proxy()
            self._proxy.rig.set_ptt(0)
            self._connected = True
        except Exception as exc:
            proxy, self._proxy = self._proxy, None
            self._connected = False
            if proxy is not None:
                try:
                    proxy("close")()
                except Exception:
                    pass
            raise BackendError(f"flrig connection failed: {exc}") from exc

    def set_ptt(self, enabled: bool) -> None:
        if not self._connected or self._proxy is None:
            raise BackendError("flrig backend is not connected")
        try:
            self._proxy.rig.set_ptt(1 if enabled else 0)
        except Exception as exc:
            raise BackendError(f"flrig PTT command failed: {exc}") from exc

    def close(self) -> None:
        self._connected = False
        proxy, self._proxy = self._proxy, None
        if proxy is not None:
            try:
                proxy("close")()
            except Exception:
                pass


def create_backend(config: BackendConfig) -> PttBackend | None:
    config.validate()
    if config.kind == "none":
        return None
    if config.kind == "serial":
        return SerialBackend(config)
    if config.kind == "rigctld":
        return RigctldBackend(config)
    if config.kind == "flrig":
        return FlrigBackend(config)
    raise BackendError(f"Unsupported PTT backend: {config.kind}")
