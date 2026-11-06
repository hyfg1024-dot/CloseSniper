import unittest
import json
import tempfile
from unittest.mock import patch
from pathlib import Path

from scripts.install_launch_agent import weekday_intervals, update_home_registration, REVIEW_LABEL


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

    def test_registration_preserves_port_and_other_projects(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            registry = home / "Applications" / "12Agent" / "programs.json"
            registry.parent.mkdir(parents=True)
            project = (home / "closesniper").resolve()
            rows = [{"id": "close-sniper", "cwd": str(project), "port": 18502, "schedule_labels": ["old"]},
                    {"id": "other", "port": 18888}]
            registry.write_text(json.dumps(rows))
            with patch("scripts.install_launch_agent.Path.home", return_value=home):
                update_home_registration(project)
            updated = json.loads(registry.read_text())
            self.assertEqual(updated[0]["port"], 18502)
            self.assertEqual(updated[1], rows[1])
            self.assertEqual(updated[0]["schedule_labels"], ["old", REVIEW_LABEL])
            self.assertEqual(len(list(registry.parent.glob("programs.before-closesniper*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
