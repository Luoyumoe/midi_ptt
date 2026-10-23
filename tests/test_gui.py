import os
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from midi_ptt_controller.config import AppConfig, BackendConfig, ConfigStore, MidiConfig
from midi_ptt_controller.engine import PttEngine
from midi_ptt_controller import gui


class _Midi:
    def __init__(self):
        self.pending = queue.Queue()

    def get_input_names(self):
        return ["Pedal"]

    def open_input(self, _name):
        return self

    def read(self, _port):
        messages = []
        while True:
            try:
                messages.append(self.pending.get_nowait())
            except queue.Empty:
                return messages

    def close(self, _port):
        pass

    def send(self, message):
        self.pending.put(message)


class _Backend:
    def connect(self):
        pass

    def set_ptt(self, _enabled):
        pass

    def close(self):
        pass


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        gui._midi_names = lambda: ["Pedal"]

    def test_settings_dialog_builds_valid_serial_config(self):
        dialog = gui.SettingsDialog(AppConfig())
        dialog.device.setCurrentIndex(dialog.device.findData("Pedal"))
        dialog.serial_port.setText("COM3")
        config = dialog.make_config()
        self.assertEqual(config.midi.device_name, "Pedal")
        self.assertEqual(config.backend.kind, "serial")
        self.assertEqual(config.backend.serial_port, "COM3")
        dialog.close()

    def test_detection_result_selects_cc_controller(self):
        dialog = gui.SettingsDialog(AppConfig())
        with patch.object(gui.QMessageBox, "information"):
            dialog._finish_detection(("Pedal", 4, "cc", 21))
        self.assertEqual(dialog.device.currentData(), "Pedal")
        self.assertEqual(dialog.channel.currentData(), 4)
        self.assertEqual(dialog.midi_mode.currentData(), "cc")
        self.assertEqual(dialog.controller.value(), 21)
        dialog.serial_port.setText("COM3")
        config = dialog.make_config()
        self.assertEqual(config.midi.mode, "cc")
        self.assertEqual(config.midi.controller, 21)
        self.assertEqual(config.midi.channel, 4)
        dialog.close()

    def test_detect_button_reads_real_mido_cc_and_updates_dialog(self):
        midi = _Midi()
        engine = PttEngine(AppConfig(), backend_factory=lambda _config: _Backend(), midi_input=midi)
        engine.start()
        dialog = gui.SettingsDialog(AppConfig(), engine=engine)
        loop = QEventLoop()
        dialog._detection_bridge.completed.connect(lambda _result: loop.quit())
        QTimer.singleShot(
            50,
            lambda: midi.send(
                SimpleNamespace(type="control_change", control=23, value=91, channel=2)
            ),
        )
        with patch.object(gui.QMessageBox, "information"):
            dialog.detect_button.click()
            QTimer.singleShot(2000, loop.quit)
            loop.exec()
        self.assertEqual(dialog.device.currentData(), "Pedal")
        self.assertEqual(dialog.channel.currentData(), 3)
        self.assertEqual(dialog.midi_mode.currentData(), "cc")
        self.assertEqual(dialog.controller.value(), 23)
        self.assertTrue(dialog.detect_button.isEnabled())
        dialog.close()
        engine.stop()

    def test_dashboard_starts_and_stops_without_hardware(self):
        config = AppConfig(
            setup_complete=True,
            midi=MidiConfig("Pedal", note=60),
            backend=BackendConfig(kind="serial", serial_port="SIM"),
        )
        engine = PttEngine(config, backend_factory=lambda _config: _Backend(), midi_input=_Midi())
        with tempfile.TemporaryDirectory() as temp:
            window = gui.MainWindow(engine, ConfigStore(Path(temp) / "config.json"), config, False)
            QTimer.singleShot(100, window._quit)
            self.app.exec()
            self.assertFalse(engine.snapshot().running)

    def test_settings_apply_runs_asynchronously_and_persists(self):
        old_config = AppConfig()
        new_config = AppConfig(
            setup_complete=True,
            midi=MidiConfig("Pedal", note=60),
            backend=BackendConfig(kind="serial", serial_port="SIM"),
        )
        engine = PttEngine(
            old_config,
            backend_factory=lambda _config: _Backend(),
            midi_input=_Midi(),
        )
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / "config.json")
            window = gui.MainWindow(engine, store, old_config, False)
            with (
                patch.object(gui.SettingsDialog, "exec", return_value=gui.QDialog.DialogCode.Accepted),
                patch.object(gui.SettingsDialog, "make_config", return_value=new_config),
            ):
                window.open_settings()
            QTimer.singleShot(500, self.app.quit)
            self.app.exec()
            self.assertFalse(window._settings_pending)
            self.assertEqual(engine.config, new_config)
            self.assertEqual(store.load(), new_config)


if __name__ == "__main__":
    unittest.main()
