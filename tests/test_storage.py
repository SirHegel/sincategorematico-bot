from pathlib import Path
import tempfile
import unittest

from sincategorematico_bot.storage import StateStore


class StateStoreActivityTests(unittest.TestCase):
    def test_activity_is_returned_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.db")
            store.add_activity("control", "primera")
            store.add_activity("security", "segunda")
            activity = store.recent_activity()
            self.assertEqual([item["message"] for item in activity], ["segunda", "primera"])
            store.close()

    def test_activity_fields_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.db")
            store.add_activity("x" * 80, "y" * 500)
            item = store.recent_activity(1)[0]
            self.assertEqual(len(str(item["kind"])), 32)
            self.assertEqual(len(str(item["message"])), 240)
            store.close()


if __name__ == "__main__":
    unittest.main()
