from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app


class AppConfigurationTests(unittest.TestCase):
    def test_valid_preset_names(self) -> None:
        for value in ("01-bienvenida", "saludo_general", "A1"):
            self.assertEqual(app.validate_preset_name(value), value)

    def test_invalid_preset_names(self) -> None:
        for value in ("", "../escape", "con espacio", "x" * 41):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.validate_preset_name(value)

    def test_ui_uses_environment_configuration(self) -> None:
        env = {
            "OTTOHABLA_HOST": "10.42.0.1",
            "OTTOHABLA_URL_HOST": "10.42.0.1",
            "OTTOHABLA_PORT": "8000",
            "OTTOHABLA_AP_SSID": "test-network",
            "OTTOHABLA_AP_PSK": "test-password",
        }
        with patch.dict(os.environ, env, clear=False):
            page = app.render_ui().decode("utf-8")
        self.assertIn("test-network", page)
        self.assertIn("test-password", page)
        self.assertIn("http://10.42.0.1:8000", page)
        for marker in ("__SSID__", "__PSK__", "__LAN_URL__", "__ROBOT_HOST__"):
            self.assertNotIn(marker, page)

    def test_qr_is_png(self) -> None:
        self.assertTrue(app.qr_png().startswith(b"\x89PNG\r\n\x1a\n"))


class BusyOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        app.force_clear_busy()
        app.BUSY_OWNER.active = False

    def tearDown(self) -> None:
        app.force_clear_busy()
        app.BUSY_OWNER.active = False

    def test_unrelated_thread_cannot_clear_busy(self) -> None:
        app.start_busy_action()
        worker = threading.Thread(target=lambda: app.set_busy(False))
        worker.start()
        worker.join()
        self.assertTrue(app.STATE["busy"])
        app.set_busy(False)
        self.assertFalse(app.STATE["busy"])


if __name__ == "__main__":
    unittest.main()
