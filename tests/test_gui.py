import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from midi_ptt_controller.config import AppConfig, BackendConfig, ConfigStore, MidiConfig
from midi_ptt_controller.engine import PttEngine
from midi_ptt_controller import gui


class _Midi:
    def get_input_names(self):
        return ["Pedal"]

    def open_input(self, _name):
        return self

    def read(self, _port):
        return []

    def close(self, _port):
        pass


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
