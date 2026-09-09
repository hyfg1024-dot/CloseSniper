import stat
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.ai_observer import AIObserverSettings, _parse_reply, analyze_watchlist, load_settings, save_settings


class AIObserverTests(unittest.TestCase):
    def test_settings_stay_local_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ai_observer.json"
            settings = AIObserverSettings(api_key="sk-example", enabled=True)
            save_settings(settings, path)
            self.assertEqual(load_settings(path), settings)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_disabled_analysis_never_calls_network(self) -> None:
        results = analyze_watchlist(
            pd.DataFrame([{"code": "600001", "name": "样本"}]), [],
            AIObserverSettings(),
        )
        self.assertEqual(results, {})

    def test_invalid_model_reply_defaults_to_caution(self) -> None:
        parsed = _parse_reply("not json")
        self.assertEqual(parsed["verdict"], "谨慎观察")
        self.assertTrue(parsed["risks"])


if __name__ == "__main__":
    unittest.main()
