import json
import os
import tempfile
import unittest
from pathlib import Path

from midi_ptt_controller.config import (
    AppConfig,
    BackendConfig,
    ConfigError,
    ConfigStore,
    MidiConfig,
)


class ConfigTests(unittest.TestCase):
    def test_missing_file_creates_safe_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / "config.json")
            config = store.load()
            self.assertFalse(config.setup_complete)
            self.assertEqual(config.backend.kind, "none")
            self.assertEqual(config.tot_seconds, 60)
            self.assertTrue(store.path.exists())

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConfigStore(Path(temp) / "config.json")
            config = AppConfig(
                setup_complete=True,
                midi=MidiConfig("USB Pedal", note=64, channel=2),
                backend=BackendConfig(kind="serial", serial_port="/dev/ttyUSB0", serial_active_low=True),
            )
            store.save(config)
            self.assertEqual(store.load(), config)
            if os.name != "nt":
                self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_bad_json_and_unknown_version_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(ConfigError):
                ConfigStore(path).load(create=False)
            path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                ConfigStore(path).load(create=False)

    def test_completed_config_requires_every_output_and_input(self):
        with self.assertRaises(ConfigError):
            AppConfig(setup_complete=True).validate()


if __name__ == "__main__":
    unittest.main()
