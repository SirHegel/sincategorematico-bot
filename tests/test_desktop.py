from __future__ import annotations

import unittest

from sincategorematico_bot.desktop import engine_card_status


class DesktopEngineStatusTests(unittest.TestCase):
    def test_a_stale_heartbeat_is_never_presented_as_running(self) -> None:
        value, note = engine_card_status(
            {
                "paused": False,
                "engine": {"alive": False, "heartbeat_at": 1_000, "status": "activo"},
                "counts": {"pending": 2, "approved": 1, "uncertain": 1},
            },
            now=1_600,
        )

        self.assertEqual(value, "Motor detenido")
        self.assertIn("SIN PULSO", note)
        self.assertIn("1 inciertos", note)

    def test_a_live_heartbeat_uses_the_configured_pause_state(self) -> None:
        value, note = engine_card_status(
            {
                "paused": True,
                "engine": {"alive": True, "heartbeat_at": 1_000, "status": "pausado"},
                "counts": {"pending": 0, "approved": 3, "uncertain": 0},
            },
            now=1_001,
        )

        self.assertEqual(value, "En pausa")
        self.assertIn("3 aprobados", note)


if __name__ == "__main__":
    unittest.main()
