import unittest

from scripts.install_launch_agent import weekday_intervals


class LaunchAgentTests(unittest.TestCase):
    def test_calendar_uses_monday_through_friday(self) -> None:
        intervals = weekday_intervals(14, 52)

        self.assertEqual([item["Weekday"] for item in intervals], [1, 2, 3, 4, 5])
        self.assertTrue(all(item["Hour"] == 14 for item in intervals))
        self.assertTrue(all(item["Minute"] == 52 for item in intervals))


if __name__ == "__main__":
    unittest.main()
