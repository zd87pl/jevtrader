import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jevtrader import config, paths


class PathsTests(unittest.TestCase):
    def test_home_override_places_every_file_under_it(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {paths.HOME_ENV: home}):
            root = Path(home)
            self.assertEqual(paths.app_dir(), root)
            self.assertEqual(paths.config_path(), root / "config.json")
            self.assertEqual(paths.ledger_path(), root / "forward.sqlite")
            self.assertEqual(paths.research_ledger_path(), root / "research.sqlite")
            self.assertEqual(paths.log_dir(), root / "logs")
            self.assertEqual(paths.lock_path(), root / "daemon.lock")
            self.assertEqual(list(root.iterdir()), [])  # Resolving paths creates nothing.

    def test_relative_home_override_is_refused(self):
        with (
            patch.dict(os.environ, {paths.HOME_ENV: "relative/dir"}),
            self.assertRaisesRegex(ValueError, paths.HOME_ENV),
        ):
            paths.app_dir()

    def test_platform_defaults(self):
        home = Path("/Users/example")
        with (
            patch.dict(os.environ, {paths.HOME_ENV: ""}),
            patch.object(Path, "home", return_value=home),
        ):
            with patch.object(paths.sys, "platform", "darwin"):
                self.assertEqual(
                    paths.app_dir(), home / "Library" / "Application Support" / paths.APP_NAME
                )
            with patch.object(paths.sys, "platform", "linux"):
                with patch.dict(os.environ, {"XDG_DATA_HOME": "/data"}):
                    self.assertEqual(paths.app_dir(), Path("/data") / paths.APP_NAME)
                with patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
                    self.assertEqual(paths.app_dir(), home / ".local" / "share" / paths.APP_NAME)

    def test_product_name_constants(self):
        self.assertEqual(paths.APP_NAME, "jevtrader")
        self.assertEqual(paths.HOME_ENV, "JEVTRADER_HOME")
        self.assertEqual(paths.LAUNCHD_LABEL, "io.github.jevtrader.daemon")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "app" / "config.json"

    def test_missing_file_loads_an_independent_copy_of_defaults(self):
        loaded = config.load(self.path)
        self.assertEqual(loaded, config.DEFAULTS)
        loaded["watchlist"].append("ABC")
        self.assertEqual(config.DEFAULTS["watchlist"], [])
        self.assertFalse(self.path.parent.exists())

    def test_default_path_follows_app_home(self):
        with patch.dict(os.environ, {paths.HOME_ENV: self.temp.name}):
            config.save({"watchlist": ["abc"]})
            self.assertEqual(config.load()["watchlist"], ["ABC"])
        self.assertTrue((Path(self.temp.name) / "config.json").exists())

    def test_save_normalizes_and_writes_owner_only_atomically(self):
        settings = {
            "sec_user_agent": "  Jane Doe jane@example.com ",
            "watchlist": ["aapl", "brk.b", "AAPL"],
            "brief_time": "8:05",
            "spend_cap_usd_month": 2,
            "local_base_url": "http://localhost:1234/v1/",
        }
        config.save(settings, self.path)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["config.json"])
        loaded = config.load(self.path)
        self.assertEqual(loaded["sec_user_agent"], "Jane Doe jane@example.com")
        # SEC's share-class form: the ticker map, and so every ledger event, writes BRK-B.
        self.assertEqual(loaded["watchlist"], ["AAPL", "BRK-B"])
        self.assertEqual(config.validate({"watchlist": ["BRK.B", "brk-b"]})["watchlist"], ["BRK-B"])
        self.assertEqual(loaded["brief_time"], "08:05")
        self.assertEqual(loaded["spend_cap_usd_month"], 2.0)
        self.assertEqual(loaded["local_base_url"], "http://localhost:1234/v1")
        self.assertEqual(set(loaded), set(config.DEFAULTS))
        self.assertEqual(json.loads(self.path.read_text()), loaded)

    def test_invalid_config_is_never_written(self):
        config.save({"watchlist": ["ABC"]}, self.path)
        before = self.path.read_text()
        with self.assertRaises(ValueError):
            config.save({"watchlist": ["not a symbol"]}, self.path)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["config.json"])

    def test_partial_file_takes_defaults_for_missing_keys(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"provider": "jev", "notify": False}))
        loaded = config.load(self.path)
        self.assertEqual((loaded["provider"], loaded["notify"]), ("jev", False))
        self.assertEqual(loaded["brief_time"], config.DEFAULTS["brief_time"])

    def test_malformed_files_fail_closed(self):
        self.path.parent.mkdir(parents=True)
        for text, message in (
            ("{not json", "not valid JSON"),
            ('{\n  "notify": tru\n}', r"not valid JSON: .* \(line 2, column 13\)"),
            ("[]", "JSON object"),
        ):
            with self.subTest(text):
                self.path.write_text(text)
                with self.assertRaisesRegex(ValueError, message):
                    config.load(self.path)

    def test_rejects_unknown_keys_and_bad_values(self):
        cases = [
            ({"api_key": "x"}, "Unknown config keys: api_key"),
            ({"version": 2}, "version"),
            ({"version": True}, "version"),
            ({"sec_user_agent": "Jane Doe"}, "sec_user_agent"),
            ({"sec_user_agent": "Jane jane@example.com\r\nX-Evil: 1"}, "sec_user_agent"),
            ({"sec_user_agent": 7}, "sec_user_agent"),
            ({"watchlist": "AAPL"}, "watchlist"),
            ({"watchlist": ["AAPL", 3]}, "watchlist"),
            ({"watchlist": ["$$$"]}, "Invalid symbol"),
            ({"watchlist": [f"S{i}" for i in range(501)]}, "limited"),
            ({"universe": "everything"}, "universe"),
            ({"provider": "gpt"}, "provider"),
            ({"provider": "openai"}, "explicit model"),
            ({"model": ""}, "model"),
            ({"model": "bad\nname"}, "model"),
            ({"model": 3}, "model"),
            ({"bars_source": "yahoo"}, "bars_source"),
            ({"alpaca_feed": "iex"}, "alpaca_feed"),
            ({"brief_time": "24:00"}, "brief_time"),
            ({"brief_time": "8:5"}, "brief_time"),
            ({"brief_time": 845}, "brief_time"),
            ({"notify": "yes"}, "notify"),
            ({"spend_cap_usd_month": -1}, "spend_cap"),
            ({"spend_cap_usd_month": True}, "spend_cap"),
            ({"spend_cap_usd_month": float("nan")}, "spend_cap"),
            ({"spend_cap_usd_month": "5"}, "spend_cap"),
            ({"spend_cap_usd_month": 1e6}, "spend_cap"),
            ({"ledger": "data/forward.sqlite"}, "absolute"),
            ({"ledger": ""}, "ledger"),
        ]
        for changes, message in cases:
            with self.subTest(changes), self.assertRaisesRegex(ValueError, message):
                config.validate(changes)
        with self.assertRaisesRegex(ValueError, "JSON object"):
            config.validate(["not", "a", "dict"])

    def test_calibrator_is_a_model_id_or_null(self):
        self.assertIsNone(config.validate({})["calibrator"])
        self.assertEqual(config.validate({"calibrator": " ridge-1 "})["calibrator"], "ridge-1")
        for bad in ("", "   ", "x" * 201, "bad\nid", 5, ["ridge-1"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "calibrator"):
                config.validate({"calibrator": bad})

    def test_local_model_url_must_stay_on_this_machine(self):
        for url in (
            "http://127.0.0.1:11434/v1",
            "http://localhost:1234/v1",
            "https://[::1]:8080/v1",
            "http://127.0.0.1/v1",
        ):
            with self.subTest(url):
                self.assertEqual(config.validate({"local_base_url": url})["local_base_url"], url)
        for url in (
            "http://example.com/v1",
            "http://127.0.0.1.example.com/v1",
            "http://user:pw@127.0.0.1:11434/v1",
            "ftp://127.0.0.1/v1",
            "http://127.0.0.1:11434/v1?x=1",
            "http://127.0.0.1:99999/v1",
            "http://10.0.0.5:11434/v1",
            "http://127.0.0.1:11434/v1%2f..",  # the engine refuses it, so config must too
            "127.0.0.1:11434",
            None,
        ):
            with self.subTest(url), self.assertRaisesRegex(ValueError, "local_base_url"):
                config.validate({"local_base_url": url})

    def test_model_and_ledger_resolution(self):
        self.assertEqual(config.resolved_model({}), "rules-v1")
        self.assertEqual(config.resolved_model({"provider": "local"}), "gpt-oss:120b")
        self.assertEqual(config.resolved_model({"provider": "jev"}), "jev-1.13.0")
        self.assertEqual(config.resolved_model({"provider": "openai", "model": " gpt-x "}), "gpt-x")
        with patch.dict(os.environ, {paths.HOME_ENV: self.temp.name}):
            self.assertEqual(config.ledger_path({}), Path(self.temp.name) / "forward.sqlite")
        custom = str(Path(self.temp.name) / "custom.sqlite")
        self.assertEqual(config.ledger_path({"ledger": custom}), Path(custom))
        expanded = config.validate({"ledger": "~/ledger.sqlite"})["ledger"]
        self.assertEqual(expanded, str(Path.home() / "ledger.sqlite"))


if __name__ == "__main__":
    unittest.main()
