from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sincategorematico_bot.runtime_paths import (
    RuntimePathError,
    fixed_runtime_path,
)


class RuntimePathTests(unittest.TestCase):
    def test_unset_or_exact_environment_value_returns_the_known_path(self) -> None:
        expected = Path("/opt/sincategorematico/config.toml")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(fixed_runtime_path("SINC_TEST_PATH", expected), expected)
        with mock.patch.dict(
            os.environ, {"SINC_TEST_PATH": str(expected)}, clear=True
        ):
            self.assertIs(fixed_runtime_path("SINC_TEST_PATH", expected), expected)

    def test_environment_cannot_redirect_access_or_use_equivalent_spelling(self) -> None:
        expected = Path("/opt/sincategorematico/config.toml")
        rejected = (
            "/etc/passwd",
            "/opt/sincategorematico/../sincategorematico/config.toml",
            "config.toml",
            f"{expected}\nX-Injected: yes",
        )
        for configured in rejected:
            with self.subTest(configured=configured):
                with mock.patch.dict(
                    os.environ, {"SINC_TEST_PATH": configured}, clear=True
                ):
                    with self.assertRaises(RuntimePathError):
                        fixed_runtime_path("SINC_TEST_PATH", expected)

    def test_rejected_path_is_not_created_or_opened(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            target = Path(raw_directory) / "must-not-exist.db"
            expected = Path("/opt/sincategorematico/state.db")
            with mock.patch.dict(
                os.environ, {"SINC_TEST_PATH": str(target)}, clear=True
            ):
                with self.assertRaises(RuntimePathError):
                    fixed_runtime_path("SINC_TEST_PATH", expected)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
