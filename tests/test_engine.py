import queue
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace

from midi_ptt_controller.config import AppConfig, BackendConfig, MidiConfig
from midi_ptt_controller.engine import PttEngine


class FakeMidi:
    def __init__(self):
        self.available = True
        self.pending = queue.Queue()

    def get_input_names(self):
        return ["Pedal"] if self.available else []

    def open_input(self, name):
        if name != "Pedal" or not self.available:
            raise RuntimeError("device absent")
        return self

    def read(self, _port):
        events = []
        while True:
            try:
                events.append(self.pending.get_nowait())
            except queue.Empty:
                return events

    def close(self, _port):
        pass

    def send(self, kind, *, note=60, control=0, value=None, velocity=127, channel=0):
        if value is None:
            value = velocity
        self.pending.put(
            SimpleNamespace(
                type=kind,
                note=note,
                control=control,
                value=value,
                velocity=velocity,
                channel=channel,
            )
        )


class FakeBackend:
    def __init__(self, *, fail_on=0, fail_off=0):
        self.states = []
        self.enabled = False
        self.connected = False
        self.closed = False
        self.fail_on = fail_on
        self.fail_off = fail_off

    def connect(self):
        self.connected = True
        self.enabled = False

    def set_ptt(self, enabled):
        if not self.connected:
            raise RuntimeError("offline")
        if enabled and self.fail_on:
            self.fail_on -= 1
            raise RuntimeError("assert failed")
        if not enabled and self.fail_off:
            self.fail_off -= 1
            raise RuntimeError("release failed")
        self.enabled = enabled
        self.states.append(enabled)

    def close(self):
        self.enabled = False
        self.connected = False
        self.closed = True


def config(*, tot=60, backend="serial"):
    backend_config = (
        BackendConfig(kind="serial", serial_port="SIM")
        if backend == "serial"
        else BackendConfig(kind="rigctld", host="radio.example", port=4532)
    )
    return AppConfig(
        setup_complete=True,
        midi=MidiConfig(device_name="Pedal", note=60, channel=1),
        backend=backend_config,
        tot_seconds=tot,
    )


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.midi = FakeMidi()
        self.backend = FakeBackend()
        self.engine = PttEngine(
            config(),
            backend_factory=lambda _config: self.backend,
            midi_input=self.midi,
            poll_interval=0.005,
            scan_interval=0.02,
        )
        self.engine.start()
        self.assertTrue(wait_for(lambda: self.engine.snapshot().midi_connected))

    def tearDown(self):
        self.engine.stop()

    def test_note_on_off_and_zero_velocity_release(self):
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: self.backend.enabled))
        self.midi.send("note_on", velocity=0)
        self.assertTrue(wait_for(lambda: not self.backend.enabled))
        self.midi.send("note_on", channel=1)
        time.sleep(0.05)
        self.assertFalse(self.backend.enabled)

    def test_disconnect_releases_active_ptt(self):
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: self.backend.enabled))
        self.midi.available = False
        self.assertTrue(wait_for(lambda: not self.engine.snapshot().midi_connected))
        self.assertTrue(wait_for(lambda: not self.backend.enabled))
        self.midi.available = True
        self.assertTrue(wait_for(lambda: self.engine.snapshot().midi_connected))
        self.midi.send("note_on")
        time.sleep(0.05)
        self.assertFalse(self.backend.enabled)
        self.midi.send("note_off")
        self.assertTrue(wait_for(lambda: not self.engine.snapshot().fault))
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: self.backend.enabled))

    def test_failed_assert_is_released_and_requires_new_press(self):
        self.backend.fail_on = 1
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: self.backend.fail_on == 0 and bool(self.backend.states)))
        self.assertFalse(self.backend.enabled)
        self.midi.send("note_off")
        self.assertTrue(wait_for(lambda: not self.engine.snapshot().pedal_down))
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: self.backend.enabled))

    def test_tot_forces_release_and_requires_pedal_release(self):
        self.engine.update_config(replace(config(tot=1), backend=config().backend))
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: self.backend.enabled))
        self.assertTrue(wait_for(lambda: not self.backend.enabled, timeout=1.7))
        self.assertIn("TOT expired", self.engine.snapshot().fault)
        self.midi.send("note_off")
        self.assertTrue(wait_for(lambda: not self.engine.snapshot().fault))

    def test_runtime_backend_switch_releases_old_before_replacing(self):
        replacement = FakeBackend()
        old = self.backend
        self.engine._backend_factory = lambda cfg: replacement if cfg.kind == "rigctld" else old
        new_config = config(backend="rigctld")
        self.engine.update_config(new_config)
        self.assertTrue(old.closed)
        self.assertTrue(replacement.connected)
        self.assertFalse(replacement.enabled)
        self.assertFalse(self.engine.snapshot().ptt_applied)

    def test_cc_on_off_controls_ptt(self):
        self.engine.config = AppConfig(
            setup_complete=True,
            midi=MidiConfig("Pedal", mode="cc", controller=12, channel=1),
            backend=BackendConfig(kind="serial", serial_port="SIM"),
        )
        self.engine.start()
        self.assertTrue(wait_for(lambda: self.engine.snapshot().midi_connected))
        self.midi.send("control_change", control=12, value=64)
        self.assertTrue(wait_for(lambda: self.engine.snapshot().ptt_applied is True))
        self.midi.send("control_change", control=12, value=0)
        self.assertTrue(wait_for(lambda: self.engine.snapshot().ptt_applied is False))

    def test_detect_control_works_before_setup_is_complete(self):
        self.engine.stop()
        detector = PttEngine(
            AppConfig(),
            backend_factory=lambda _config: self.backend,
            midi_input=self.midi,
            poll_interval=0.005,
            scan_interval=0.02,
        )
        detector.start()
        try:
            result_box = []
            thread = __import__("threading").Thread(
                target=lambda: result_box.append(detector.detect_midi(timeout=2.0)), daemon=True
            )
            thread.start()
            self.assertTrue(wait_for(lambda: detector.snapshot().midi_connected))
            self.midi.send("control_change", control=23, value=0, channel=2)
            thread.join(2.0)
            self.assertEqual(result_box, [("Pedal", 3, "cc", 23)])
            self.assertTrue(wait_for(lambda: not detector.snapshot().midi_connected))
        finally:
            detector.stop()

    def test_detection_press_does_not_assert_ptt(self):
        result_box = []
        thread = __import__("threading").Thread(
            target=lambda: result_box.append(self.engine.detect_midi(timeout=2.0, device_name="Pedal")),
            daemon=True,
        )
        thread.start()
        self.assertTrue(wait_for(lambda: self.engine._detect_request is not None))
        self.midi.send("note_on", note=60, velocity=127)
        thread.join(2.0)
        self.assertEqual(result_box, [("Pedal", 1, "note", 60)])
        self.assertFalse(self.backend.enabled)

    def test_switch_while_pedal_down_never_transfers_ptt(self):
        replacement = FakeBackend()
        old = self.backend
        self.engine._backend_factory = lambda cfg: replacement if cfg.kind == "rigctld" else old
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: old.enabled))
        new_config = config(backend="rigctld")
        self.engine.update_config(new_config)
        self.assertFalse(old.enabled)
        self.assertFalse(replacement.enabled)
        self.assertTrue(self.engine.snapshot().pedal_down)
        self.assertFalse(self.engine.snapshot().ptt_applied)
        self.midi.send("note_off")
        self.assertTrue(wait_for(lambda: not self.engine.snapshot().pedal_down))
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: replacement.enabled))

    def test_failed_release_aborts_runtime_backend_switch(self):
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: self.backend.enabled))
        self.backend.fail_off = 1
        new_config = config(backend="rigctld")
        with self.assertRaises(RuntimeError):
            self.engine.update_config(new_config)
        self.assertFalse(self.backend.closed)
        self.assertTrue(self.engine.snapshot().setup_complete)
        self.assertEqual(self.engine.config.backend.kind, "serial")

    def test_shutdown_releases_and_closes_backend(self):
        self.midi.send("note_on")
        self.assertTrue(wait_for(lambda: self.backend.enabled))
        self.engine.stop()
        self.assertFalse(self.backend.enabled)
        self.assertTrue(self.backend.closed)

    def test_core_import_does_not_load_qt(self):
        script = (
            "import sys; from midi_ptt_controller import config, backends, engine; "
            "assert not any(n == 'PySide6' or n.startswith('PySide6.') for n in sys.modules)"
        )
        subprocess.run([sys.executable, "-c", script], check=True)

    def test_main_headless_import_does_not_load_qt(self):
        script = (
            "import sys; import midi_ptt_controller.main; "
            "assert not any(n == 'PySide6' or n.startswith('PySide6.') for n in sys.modules)"
        )
        subprocess.run([sys.executable, "-c", script], check=True)


if __name__ == "__main__":
    unittest.main()
