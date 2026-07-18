from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hpsync import cli as hpsync


class ConfigTests(unittest.TestCase):
    def test_keyboard_interrupt_is_a_clean_cancellation(self) -> None:
        error_output = io.StringIO()
        with (
            mock.patch.object(hpsync, "config_wizard", side_effect=KeyboardInterrupt),
            contextlib.redirect_stderr(error_output),
        ):
            result = hpsync.main(["config"])

        self.assertEqual(result, 130)
        self.assertIn("Cancelled", error_output.getvalue())

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

    def test_wizard_always_adds_an_explained_local_copy(self) -> None:
        answers = iter(
            [
                "project",
                "/tmp/local-project",
                "",
                "",
                "user@cluster.example.org",
                "",
                "/work/project",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            output = io.StringIO()
            with (
                mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)),
                mock.patch.object(hpsync, "bootstrap_config") as bootstrap,
                contextlib.redirect_stdout(output),
            ):
                hpsync.config_wizard(path)

            config = hpsync.load_config(path)
            locations = config["repositories"][0]["locations"]
            self.assertEqual(
                locations[0],
                {
                    "name": "local",
                    "transport": "local",
                    "path": "/tmp/local-project",
                    "state": "~/.local/state/hpsync",
                },
            )
            self.assertEqual(locations[1]["name"], "cluster")
            self.assertIn("short label", output.getvalue())
            self.assertIn("safety copies", output.getvalue())
            self.assertIn("-" * 71, output.getvalue())
            bootstrap.assert_called_once_with(config)
            self.assertIn("Setup always includes a local copy", output.getvalue())

    def test_wizard_can_go_back_and_change_a_conditional_answer(self) -> None:
        answers = iter(
            [
                "project",
                "/tmp/local-project",
                "",
                "ssh",
                "back",
                "local",
                "",
                "/tmp/second-project",
                "",
                "",
                "",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            output = io.StringIO()
            with (
                mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)),
                mock.patch.object(hpsync, "bootstrap_config", return_value=True),
                contextlib.redirect_stdout(output),
            ):
                hpsync.config_wizard(path)

            locations = hpsync.load_config(path)["repositories"][0]["locations"]
            self.assertEqual(
                locations[1],
                {
                    "name": "local-2",
                    "transport": "local",
                    "path": "/tmp/second-project",
                    "state": "~/.local/state/hpsync",
                },
            )
            self.assertIn("Returning to the previous question", output.getvalue())
            self.assertIn("Type 'back'", output.getvalue())


if __name__ == "__main__":
    unittest.main()
