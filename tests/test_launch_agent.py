import unittest
from pathlib import Path

from scripts.install_launch_agent import weekday_intervals


class LaunchAgentTests(unittest.TestCase):
    def test_calendar_uses_monday_through_friday(self) -> None:
        intervals = weekday_intervals(14, 52)

        self.assertEqual([item["Weekday"] for item in intervals], [1, 2, 3, 4, 5])
        self.assertTrue(all(item["Hour"] == 14 for item in intervals))
        self.assertTrue(all(item["Minute"] == 52 for item in intervals))

    def test_installer_preserves_local_config_directory(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "install_launch_agent.py"
        source = script.read_text(encoding="utf-8")

        self.assertIn('"--exclude", "config/"', source)


if __name__ == "__main__":
    unittest.main()
