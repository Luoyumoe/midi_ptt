"""MIDI monitor, PTT state machine and serialized output worker. No Qt dependency."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .backends import PttBackend, create_backend
from .config import AppConfig

LOG = logging.getLogger(__name__)


class MidiInput(Protocol):
    def get_input_names(self) -> list[str]: ...
    def open_input(self, name: str) -> Any: ...
    def read(self, port: Any) -> list[Any]: ...
    def close(self, port: Any) -> None: ...


class MidoInput:
    def get_input_names(self) -> list[str]:
        import mido

        return list(mido.get_input_names())

    def open_input(self, name: str) -> Any:
        import mido

        return mido.open_input(name)

    def read(self, port: Any) -> list[Any]:
        return list(port.iter_pending())

    def close(self, port: Any) -> None:
        port.close()


@dataclass(frozen=True)
class PttStatus:
    running: bool = False
    setup_complete: bool = False
    midi_connected: bool = False
    midi_device: str = ""
    pedal_down: bool = False
    ptt_requested: bool = False
    ptt_applied: bool | None = False
    tot_elapsed: float = 0.0
    tot_limit: int = 60
    fault: str = ""


@dataclass(frozen=True)
class EngineEvent:
    timestamp: float
    level: str
    message: str


class PttEngine:
    """Owns MIDI and PTT I/O on one worker thread; GUI consumes snapshots/events."""

    def __init__(
        self,
        config: AppConfig,
        *,
        backend_factory: Callable[[Any], PttBackend | None] = create_backend,
        midi_input: MidiInput | None = None,
        poll_interval: float = 0.02,
        scan_interval: float = 2.0,
    ) -> None:
        self.config = config
        self._backend_factory = backend_factory
        self._midi: MidiInput = midi_input or MidoInput()
        self._poll_interval = poll_interval
        self._scan_interval = scan_interval
        self._commands: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.events: queue.Queue[EngineEvent] = queue.Queue(maxsize=512)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._status = PttStatus(setup_complete=config.setup_complete, tot_limit=config.tot_seconds)
        self._backend: PttBackend | None = None
        self._backend_connected = False
        self._midi_port: Any | None = None
        self._midi_connected = False
        self._pedal_down = False
        self._suppress_until_release = False
        self._release_mapping: tuple[int | None, int | None] | None = None
        self._test_until = 0.0
        self._ptt_started: float | None = None
        self._applied: bool | None = False
        self._fault = ""
        self._last_scan = 0.0
        self._last_backend_attempt = 0.0
        self._last_off_attempt = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._fault = ""
        self._pedal_down = False
        self._suppress_until_release = False
        self._release_mapping = None
        self._ptt_started = None
        self._applied = False
        self._thread = threading.Thread(target=self._run, name="ptt-io", daemon=False)
        self._thread.start()

    def snapshot(self) -> PttStatus:
        with self._state_lock:
            return self._status

    def _publish(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._ptt_started) if self._ptt_started is not None else 0.0
        status = PttStatus(
            running=bool(self._thread and self._thread.is_alive() and not self._stop.is_set()),
            setup_complete=self.config.setup_complete,
            midi_connected=self._midi_connected,
            midi_device=self.config.midi.device_name,
            pedal_down=self._pedal_down,
            ptt_requested=(self._pedal_down and not self._suppress_until_release)
            or self._test_until > now,
            ptt_applied=self._applied,
            tot_elapsed=elapsed,
            tot_limit=self.config.tot_seconds,
            fault=self._fault or (
                "Release pedal to re-arm" if self._suppress_until_release else ""
            ),
        )
        with self._state_lock:
            self._status = status

    def _emit(self, level: str, message: str) -> None:
        getattr(
            LOG,
            level if level in {"debug", "info", "warning", "error", "critical"} else "info",
        )(message)
        event = EngineEvent(time.time(), level, message)
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass

    def submit_midi_message(self, message: Any) -> None:
        """Inject an input event; intended for simulator and deterministic tests."""
        self._commands.put(("midi", message))

    def update_config(self, config: AppConfig, timeout: float = 65.0) -> None:
        config.validate()
        result = threading.Event()
        answer: list[BaseException | None] = []
        self._commands.put(("configure", config, result, answer))
        if not result.wait(timeout):
            raise TimeoutError("Timed out while applying settings")
        if answer and answer[0] is not None:
            raise RuntimeError(str(answer[0])) from answer[0]

    def test_ptt(self, duration: float = 0.5, timeout: float = 2.0) -> None:
        result = threading.Event()
        answer: list[BaseException | None] = []
        self._commands.put(("test", min(max(duration, 0.1), 1.0), result, answer))
        if not result.wait(timeout):
            raise TimeoutError("Timed out while starting PTT test")
        if answer and answer[0] is not None:
            raise RuntimeError(str(answer[0])) from answer[0]

    def _run(self) -> None:
        self._emit("info", "PTT engine starting")
        self._last_scan = 0.0
        self._last_backend_attempt = 0.0
        try:
            self._initialize_backend()
            if not self.config.setup_complete:
                self._emit("warning", "Setup incomplete; PTT output disabled")
            elif not self.config.midi.device_name:
                self._emit("warning", "No MIDI input selected; waiting for settings")
            while not self._stop.is_set():
                self._process_commands()
                now = time.monotonic()
                self._scan_midi(now)
                self._read_midi()
                self._watch_tot(now)
                self._retry_backend(now)
                self._apply_target(now)
                self._publish()
                self._stop.wait(self._poll_interval)
        except Exception as exc:
            self._fault = f"PTT engine worker failed: {exc}"
            self._emit("critical", self._fault)
        finally:
            self._stop.set()
            self._shutdown_backend()
            self._close_midi()
            self._applied = False if self._applied is not None else None
            self._publish()
            self._emit("info", "PTT engine stopped")

    def _initialize_backend(self) -> None:
        if not self.config.setup_complete or self.config.backend.kind == "none":
            return
        try:
            self._backend = self._backend_factory(self.config.backend)
            if self._backend is not None:
                self._backend.connect()
                self._backend_connected = True
                self._applied = False
                self._emit("info", f"PTT backend connected: {self.config.backend.kind}")
        except Exception as exc:
            self._backend_connected = False
            self._applied = None
            self._fault = f"PTT backend unavailable: {exc}"
            self._emit("error", self._fault)
            self._last_backend_attempt = time.monotonic()

    def _scan_midi(self, now: float) -> None:
        if not self.config.setup_complete or not self.config.midi.device_name:
            return
        if now - self._last_scan < self._scan_interval:
            return
        self._last_scan = now
        try:
            names = self._midi.get_input_names()
        except Exception as exc:
            self._disconnect_midi(f"MIDI scan failed: {exc}")
            return
        wanted = self.config.midi.device_name
        if self._midi_port is not None and (wanted not in names or not self._midi_connected):
            self._disconnect_midi("MIDI input disconnected")
        if self._midi_port is None and wanted in names:
            try:
                self._midi_port = self._midi.open_input(wanted)
                self._midi_connected = True
                self._emit("info", f"MIDI input connected: {wanted}")
                self._pedal_down = False
            except Exception as exc:
                self._midi_port = None
                self._midi_connected = False
                self._emit("warning", f"Cannot open MIDI input {wanted}: {exc}")
        elif self._midi_port is None:
            self._midi_connected = False

    def _disconnect_midi(self, message: str) -> None:
        if self._midi_port is not None:
            try:
                self._midi.close(self._midi_port)
            except Exception:
                pass
        self._midi_port = None
        was_connected = self._midi_connected
        self._midi_connected = False
        if was_connected or self._pedal_down:
            mapping = (self.config.midi.note, self.config.midi.channel)
            self._pedal_down = False
            self._suppress_until_release = True
            self._release_mapping = mapping
            self._test_until = 0.0
            if not self._fault.startswith("TOT expired"):
                self._fault = "MIDI state unknown; release pedal to re-arm"
            self._emit("warning", f"{message}; PTT release requested")

    def _read_midi(self) -> None:
        if self._midi_port is None:
            return
        try:
            for message in self._midi.read(self._midi_port):
                self._handle_midi(message)
        except Exception as exc:
            self._disconnect_midi(f"MIDI input read failed: {exc}")

    def _handle_midi(self, message: Any) -> None:
        try:
            msg_type = message.type
            note = int(message.note)
            velocity = int(getattr(message, "velocity", 0))
            channel = int(getattr(message, "channel", 0)) + 1
        except (AttributeError, TypeError, ValueError):
            return
        if msg_type not in {"note_on", "note_off"}:
            return
        current_match = note == self.config.midi.note and (
            self.config.midi.channel is None or channel == self.config.midi.channel
        )
        release_match = self._release_mapping is not None and note == self._release_mapping[0] and (
            self._release_mapping[1] is None or channel == self._release_mapping[1]
        )
        if not current_match and not release_match:
            return
        is_down = msg_type == "note_on" and velocity > 0
        if not is_down:
            was_down = self._pedal_down
            self._pedal_down = False
            self._suppress_until_release = False
            self._release_mapping = None
            if self._fault.startswith("TOT expired"):
                self._fault = ""
                self._emit("info", "TOT reset after pedal release")
            elif self._fault.startswith("MIDI state unknown"):
                self._fault = ""
                self._emit("info", "MIDI state re-armed after pedal release")
            elif was_down:
                self._emit("info", "Pedal released")
            return
        if not current_match:
            return
        if not self._pedal_down:
            self._emit("info", "Pedal pressed")
        self._pedal_down = True
        if self._test_until > time.monotonic():
            self._suppress_until_release = True
        if self._fault and not self._fault.startswith("TOT expired"):
            self._suppress_until_release = True

    def _process_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            kind = command[0]
            if kind == "midi":
                self._handle_midi(command[1])
            elif kind == "configure":
                _, config, result, answer = command
                try:
                    self._apply_config(config)
                    answer.append(None)
                except Exception as exc:
                    answer.append(exc)
                finally:
                    result.set()
            elif kind == "test":
                _, duration, result, answer = command
                try:
                    if self._pedal_down:
                        raise RuntimeError("Release the pedal before testing PTT")
                    if self._fault or self._backend is None or not self._backend_connected:
                        raise RuntimeError("PTT backend is not ready")
                    self._test_until = time.monotonic() + duration
                    self._emit("warning", f"PTT test pulse started ({duration:.1f}s)")
                    answer.append(None)
                except Exception as exc:
                    answer.append(exc)
                finally:
                    result.set()

    def _apply_config(self, config: AppConfig) -> None:
        old_config = self.config
        was_pedal_down = self._pedal_down
        backend_changed = old_config.backend != config.backend or (
            old_config.setup_complete != config.setup_complete
        )
        self._pedal_down = was_pedal_down
        self._suppress_until_release = was_pedal_down
        self._release_mapping = (
            (old_config.midi.note, old_config.midi.channel) if was_pedal_down else None
        )
        self._test_until = 0.0
        if backend_changed:
            if self._backend is not None:
                try:
                    self._backend.set_ptt(False)
                    self._applied = False
                except Exception as exc:
                    self._fault = f"Cannot release old PTT backend; switch aborted: {exc}"
                    self._suppress_until_release = True
                    raise RuntimeError(self._fault) from exc
            replacement: PttBackend | None = None
            if config.setup_complete and config.backend.kind != "none":
                replacement = self._backend_factory(config.backend)
                if replacement is not None:
                    try:
                        replacement.connect()
                    except Exception:
                        replacement.close()
                        raise
            old_backend = self._backend
            self._backend = replacement
            self._backend_connected = replacement is not None
            self._applied = False if replacement is not None else False
            if old_backend is not None:
                old_backend.close()
            if not self._fault.startswith("MIDI state unknown"):
                self._fault = ""
            self._emit("info", f"PTT backend changed to {config.backend.kind}")
        if old_config.midi.device_name != config.midi.device_name:
            self._disconnect_midi("MIDI selection changed")
            self._release_mapping = (config.midi.note, config.midi.channel)
            self._suppress_until_release = True
        if was_pedal_down and not self._fault:
            self._fault = "MIDI state unknown; release pedal to re-arm"
        self.config = config
        self._last_scan = 0.0
        self._publish()

    def _retry_backend(self, now: float) -> None:
        if self._backend is None or self._backend_connected or now - self._last_backend_attempt < 2.0:
            return
        self._last_backend_attempt = now
        try:
            self._backend.connect()
            self._backend_connected = True
            self._applied = False
            if not self._fault.startswith(("TOT expired", "MIDI state unknown")):
                self._fault = ""
            if self._pedal_down:
                self._suppress_until_release = True
            self._emit("info", "PTT backend connection restored; release pedal before transmitting")
        except Exception as exc:
            self._fault = f"PTT backend unavailable: {exc}"
            self._emit("warning", self._fault)

    def _watch_tot(self, now: float) -> None:
        if self._ptt_started is None:
            return
        if now - self._ptt_started >= self.config.tot_seconds:
            self._test_until = 0.0
            self._suppress_until_release = True
            if not self._fault.startswith("TOT expired"):
                self._fault = "TOT expired; release pedal to reset"
                self._emit("error", self._fault)

    def _target(self, now: float) -> bool:
        pedal_request = self._pedal_down and not self._suppress_until_release
        return bool(
            self.config.setup_complete
            and self._backend is not None
            and self._backend_connected
            and not self._fault
            and (pedal_request or self._test_until > now)
        )

    def _apply_target(self, now: float) -> None:
        if self._backend is None or not self._backend_connected:
            return
        target = self._target(now)
        if target == self._applied:
            if not target and self._applied is None and now - self._last_off_attempt >= 1.0:
                self._try_release(now)
            return
        try:
            self._backend.set_ptt(target)
            self._applied = target
            if target:
                self._ptt_started = now
                self._emit("info", "PTT asserted")
            else:
                self._ptt_started = None
                self._emit("info", "PTT released")
                if self._fault and not self._fault.startswith(("TOT expired", "MIDI state unknown")):
                    self._fault = ""
                    self._emit("info", "PTT backend recovered")
        except Exception as exc:
            self._applied = None
            self._ptt_started = None
            self._suppress_until_release = True
            self._fault = f"PTT output failed; release is being retried: {exc}"
            self._last_off_attempt = now
            self._emit("error", self._fault)
            self._try_release(now)

    def _try_release(self, now: float) -> None:
        if self._backend is None or not self._backend_connected:
            return
        self._last_off_attempt = now
        try:
            self._backend.set_ptt(False)
            self._applied = False
            self._ptt_started = None
            self._backend_connected = True
            if self._fault and not self._fault.startswith(("TOT expired", "MIDI state unknown")):
                self._fault = ""
                self._emit("info", "PTT backend recovered")
        except Exception as exc:
            self._applied = None
            self._backend_connected = False
            self._fault = f"PTT release failed; reconnecting: {exc}"
            self._emit("error", self._fault)

    def _shutdown_backend(self) -> None:
        backend = self._backend
        if backend is None:
            return
        deadline = time.monotonic() + max(3.0, self.config.backend.timeout_seconds * 2)
        released = False
        while time.monotonic() < deadline:
            try:
                if self._backend_connected:
                    backend.set_ptt(False)
                else:
                    # Backend connect contract initializes output in the released state.
                    backend.connect()
                    self._backend_connected = True
                released = True
                self._applied = False
                break
            except Exception as exc:
                self._applied = None
                self._backend_connected = False
                self._emit("error", f"Shutdown PTT release failed; retrying: {exc}")
                time.sleep(0.5)
        if not released:
            self._emit("critical", "PTT release could not be confirmed during shutdown")
        try:
            backend.close()
        except Exception as exc:
            self._emit("warning", f"PTT backend close failed: {exc}")
        self._backend = None
        self._backend_connected = False

    def _close_midi(self) -> None:
        if self._midi_port is not None:
            try:
                self._midi.close(self._midi_port)
            except Exception as exc:
                self._emit("warning", f"MIDI input close failed: {exc}")
        self._midi_port = None
        self._midi_connected = False

    def stop(self, timeout: float = 100.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("PTT engine worker did not stop in time")
