"""CLI entry point. GUI imports happen only in GUI mode."""

from __future__ import annotations

import argparse
import atexit
import logging
from logging.handlers import RotatingFileHandler
import queue
import signal
import sys
import threading
import time
from typing import Any

from .backends import PttBackend
from .config import AppConfig, BackendConfig, ConfigError, ConfigStore, MidiConfig
from .engine import PttEngine


def configure_logging(store: ConfigStore, *, headless: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler(sys.stderr if headless else sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        log_path = store.path.parent / "logs" / "controller.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        rotating.setFormatter(formatter)
        root.addHandler(rotating)
    except OSError as exc:
        root.warning("File logging unavailable: %s", exc)


def _read_config(store: ConfigStore) -> tuple[AppConfig, str]:
    try:
        return store.load(), ""
    except ConfigError as exc:
        logging.getLogger(__name__).error("Configuration rejected; PTT remains disabled: %s", exc)
        return AppConfig(), str(exc)


class _SimulatedPort:
    def __init__(self) -> None:
        self.messages: queue.Queue[Any] = queue.Queue()


class _SimulatedMidi:
    def __init__(self) -> None:
        self.available = True

    def get_input_names(self) -> list[str]:
        return ["Simulated MIDI"] if self.available else []

    def open_input(self, name: str) -> _SimulatedPort:
        if name != "Simulated MIDI" or not self.available:
            raise RuntimeError("Simulated device unavailable")
        return _SimulatedPort()

    def read(self, port: _SimulatedPort) -> list[Any]:
        messages = []
        while True:
            try:
                messages.append(port.messages.get_nowait())
            except queue.Empty:
                return messages

    def close(self, port: _SimulatedPort) -> None:
        del port


class _SimulatedBackend(PttBackend):
    def __init__(self) -> None:
        self.enabled = False
        self.connected = False

    def connect(self) -> None:
        self.enabled = False
        self.connected = True

    def set_ptt(self, enabled: bool) -> None:
        if not self.connected:
            raise RuntimeError("simulator backend is disconnected")
        self.enabled = enabled

    def close(self) -> None:
        self.enabled = False
        self.connected = False


def _run_simulator() -> int:
    import mido

    config = AppConfig(
        setup_complete=True,
        midi=MidiConfig(device_name="Simulated MIDI", note=60, channel=None),
        backend=BackendConfig(kind="serial", serial_port="SIMULATED"),
        tot_seconds=60,
    )
    midi = _SimulatedMidi()
    backend = _SimulatedBackend()
    engine = PttEngine(config, backend_factory=lambda _config: backend, midi_input=midi)
    engine.start()
    stop_printer = threading.Event()

    def print_events() -> None:
        while not stop_printer.is_set():
            try:
                event = engine.events.get(timeout=0.2)
            except queue.Empty:
                continue
            print(f"{event.level.upper():8} {event.message}", flush=True)

    printer = threading.Thread(target=print_events, name="sim-event-printer", daemon=True)
    printer.start()
    print("Simulator only; no hardware access. Commands: press, release, disconnect, reconnect, status, quit")

    def wait_for(predicate, timeout: float = 2.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    try:
        while True:
            try:
                command = input("sim> ").strip().lower()
            except EOFError:
                break
            if command == "press":
                engine.submit_midi_message(mido.Message("note_on", note=60, velocity=127))
                wait_for(lambda: engine.snapshot().ptt_applied is True)
            elif command == "release":
                engine.submit_midi_message(mido.Message("note_off", note=60, velocity=0))
                wait_for(lambda: engine.snapshot().ptt_applied is False)
            elif command == "disconnect":
                midi.available = False
                wait_for(lambda: not engine.snapshot().midi_connected)
                print("Simulated MIDI disconnected")
            elif command == "reconnect":
                midi.available = True
                wait_for(lambda: engine.snapshot().midi_connected)
                print("Simulated MIDI available")
            elif command == "status":
                status = engine.snapshot()
                print(
                    f"midi={status.midi_connected} pedal={status.pedal_down} "
                    f"ptt={status.ptt_applied} TOT={status.tot_elapsed:.1f}/{status.tot_limit}s "
                    f"fault={status.fault or '-'}"
                )
            elif command in {"quit", "exit"}:
                break
            elif command:
                print("Unknown command")
    except KeyboardInterrupt:
        print()
    finally:
        engine.stop()
        stop_printer.set()
        printer.join(timeout=1)
    return 0


def _run_headless(engine: PttEngine) -> int:
    stopped = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stopped.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass
    engine.start()
    atexit.register(engine.stop)
    try:
        while not stopped.wait(0.5):
            pass
    except KeyboardInterrupt:
        stopped.set()
    finally:
        engine.stop()
    return 0


def _run_gui(
    engine: PttEngine,
    store: ConfigStore,
    config: AppConfig,
    minimized: bool,
    startup_error: str = "",
) -> int:
    # Keep PySide6 out of imports on --headless and --simulate paths.
    from .gui import run_gui

    return run_gui(engine, store, config, minimized=minimized, startup_error=startup_error)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MIDI pedal to ham radio PTT controller")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--headless", action="store_true", help="run without GUI")
    modes.add_argument("--minimized", action="store_true", help="start GUI minimized to tray")
    modes.add_argument("--simulate", action="store_true", help="run interactive no-hardware simulator")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.simulate:
        logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(message)s")
        return _run_simulator()
    store = ConfigStore()
    configure_logging(store, headless=args.headless)
    config, startup_error = _read_config(store)
    engine = PttEngine(config)
    if args.headless:
        return _run_headless(engine)
    return _run_gui(engine, store, config, minimized=args.minimized, startup_error=startup_error)


if __name__ == "__main__":
    raise SystemExit(main())
