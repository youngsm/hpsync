from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import hpsync


class ConfigTests(unittest.TestCase):
    def test_adds_local_and_ssh_locations(self) -> None:
        config = hpsync.default_config()
        hpsync.add_repo(config, "project")
        hpsync.add_location(
            config,
            "project",
            {"name": "laptop", "transport": "local", "path": "/tmp/project"},
        )
        hpsync.add_location(
            config,
            "project",
            {
                "name": "cluster",
                "transport": "ssh",
                "host": "cluster",
                "path": "/work/project",
                "auth_command": ["sshproxy"],
            },
        )

        hpsync.validate_config(config)
        self.assertEqual(len(config["repositories"][0]["locations"]), 2)

    def test_rejects_duplicates_and_blank_names(self) -> None:
        config = hpsync.default_config()
        hpsync.add_repo(config, "project")
        with self.assertRaises(hpsync.ConfigError):
            hpsync.add_repo(config, "project")
        with self.assertRaises(hpsync.ConfigError):
            hpsync.add_repo(config, " ")

    def test_replaces_and_removes_location(self) -> None:
        config = hpsync.default_config()
        hpsync.add_repo(config, "project")
        hpsync.add_location(
            config,
            "project",
            {"name": "local", "transport": "local", "path": "/old"},
        )
        hpsync.add_location(
            config,
            "project",
            {"name": "local", "transport": "local", "path": "/new"},
            replace=True,
        )
        self.assertEqual(config["repositories"][0]["locations"][0]["path"], "/new")

        hpsync.remove_location(config, "project", "local")
        self.assertEqual(config["repositories"][0]["locations"], [])

    def test_config_path_uses_hpsync_environment_variable(self) -> None:
        previous = os.environ.get("HPSYNC_CONFIG")
        try:
            os.environ["HPSYNC_CONFIG"] = "/tmp/custom-hpsync.json"
            self.assertEqual(hpsync.get_config_path(), Path("/tmp/custom-hpsync.json"))
        finally:
            if previous is None:
                os.environ.pop("HPSYNC_CONFIG", None)
            else:
                os.environ["HPSYNC_CONFIG"] = previous

    def test_save_and_load_round_trip(self) -> None:
        config = hpsync.default_config()
        hpsync.add_repo(config, "project")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            hpsync.save_config(config, path)
            self.assertEqual(hpsync.load_config(path), config)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
