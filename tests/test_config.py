from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hpsync import cli as hpsync


class ConfigTests(unittest.TestCase):
    def test_validation_rejects_malformed_configs(self) -> None:
        valid_location = {
            "name": "local",
            "transport": "local",
            "path": "/tmp/project",
        }
        cases = {
            "non-object config": [],
            "unsupported version": {"version": 2, "repositories": []},
            "repositories is not a list": {"version": 1, "repositories": {}},
            "non-object repository": {"version": 1, "repositories": ["project"]},
            "blank repository name": {
                "version": 1,
                "repositories": [{"name": " ", "locations": []}],
            },
            "duplicate repositories": {
                "version": 1,
                "repositories": [
                    {"name": "project", "locations": []},
                    {"name": "project", "locations": []},
                ],
            },
            "locations is not a list": {
                "version": 1,
                "repositories": [{"name": "project", "locations": {}}],
            },
            "non-object location": {
                "version": 1,
                "repositories": [{"name": "project", "locations": ["local"]}],
            },
            "missing location path": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [{"name": "local", "transport": "local"}],
                    }
                ],
            },
            "invalid transport": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [{**valid_location, "transport": "ftp"}],
                    }
                ],
            },
            "ssh without host": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [{**valid_location, "transport": "ssh"}],
                    }
                ],
            },
            "invalid ssh port": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [
                            {
                                **valid_location,
                                "transport": "ssh",
                                "host": "example.com",
                                "port": 0,
                            }
                        ],
                    }
                ],
            },
            "blank state": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [{**valid_location, "state": ""}],
                    }
                ],
            },
            "blank python": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [{**valid_location, "python": ""}],
                    }
                ],
            },
            "invalid auth command": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [{**valid_location, "auth_command": [""]}],
                    }
                ],
            },
            "duplicate locations": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [valid_location, valid_location],
                    }
                ],
            },
            "invalid exclusions": {
                "version": 1,
                "repositories": [
                    {
                        "name": "project",
                        "locations": [],
                        "exclude_parts": ".git",
                    }
                ],
            },
        }

        for label, config in cases.items():
            with self.subTest(label), self.assertRaises(hpsync.ConfigError):
                hpsync.validate_config(config)

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

    def test_mutators_and_selection_report_unknown_or_duplicate_names(self) -> None:
        config = hpsync.default_config()
        hpsync.add_repo(config, "project")
        location = {"name": "local", "transport": "local", "path": "/tmp/project"}
        hpsync.add_location(config, "project", location)

        with self.assertRaisesRegex(hpsync.ConfigError, "already exists"):
            hpsync.add_location(config, "project", location)
        with self.assertRaisesRegex(hpsync.ConfigError, "Unknown repository"):
            hpsync.remove_repo(config, "missing")
        with self.assertRaisesRegex(hpsync.ConfigError, "Unknown location"):
            hpsync.remove_location(config, "project", "missing")
        with self.assertRaisesRegex(hpsync.ConfigError, "Unknown repository"):
            hpsync.select_repositories(config, ["missing"])
        with self.assertRaisesRegex(hpsync.ConfigError, "no location"):
            hpsync.select_locations(config["repositories"][0], ["missing"])

        self.assertEqual(hpsync.select_repositories(config, None), config["repositories"])
        selected = hpsync.select_locations(config["repositories"][0], ["local"])
        self.assertEqual(selected[0], hpsync.Location("local", "/tmp/project", "local"))

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

    def test_load_handles_missing_and_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            self.assertEqual(hpsync.load_config(missing, allow_missing=True), hpsync.default_config())
            with self.assertRaisesRegex(hpsync.ConfigError, "No config"):
                hpsync.load_config(missing)

            invalid = Path(directory) / "invalid.json"
            invalid.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(hpsync.ConfigError, "Invalid JSON"):
                hpsync.load_config(invalid)

    def test_config_cli_supports_the_full_editing_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            prefix = ["--config", str(path), "config"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(hpsync.main([*prefix, "add-repo", "project"]), 0)
                self.assertEqual(
                    hpsync.main(
                        [
                            *prefix,
                            "add-location",
                            "project",
                            "laptop",
                            "--path",
                            "/tmp/project",
                            "--local",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    hpsync.main(
                        [
                            *prefix,
                            "add-location",
                            "project",
                            "cluster",
                            "--path",
                            "/work/project",
                            "--ssh",
                            "user@cluster.example.org",
                            "--port",
                            "2222",
                            "--state",
                            "/scratch/hpsync",
                            "--python",
                            "python3.11",
                            "--auth-command",
                            "kinit -R",
                        ]
                    ),
                    0,
                )
                self.assertEqual(hpsync.main([*prefix, "validate"]), 0)
                self.assertEqual(hpsync.main([*prefix, "show"]), 0)
                self.assertEqual(hpsync.main([*prefix, "path"]), 0)

            config = hpsync.load_config(path)
            remote = config["repositories"][0]["locations"][1]
            self.assertEqual(remote["port"], 2222)
            self.assertEqual(remote["python"], "python3.11")
            self.assertEqual(remote["auth_command"], ["kinit", "-R"])
            self.assertIn('"cluster"', output.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    hpsync.main([*prefix, "remove-location", "project", "cluster"]), 0
                )
                self.assertEqual(hpsync.main([*prefix, "remove-repo", "project"]), 0)
            self.assertEqual(hpsync.load_config(path)["repositories"], [])

    def test_config_cli_reports_invalid_local_ssh_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = hpsync.default_config()
            hpsync.add_repo(config, "project")
            hpsync.save_config(config, path)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                result = hpsync.main(
                    [
                        "--config",
                        str(path),
                        "config",
                        "add-location",
                        "project",
                        "local",
                        "--path",
                        "/tmp/project",
                        "--local",
                        "--port",
                        "22",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("require --ssh", errors.getvalue())

    def test_config_cli_can_show_a_missing_config_and_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = hpsync.main(
                    ["--config", str(path), "config", "show", "--allow-missing"]
                )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue()), hpsync.default_config())

            config = hpsync.default_config()
            hpsync.add_repo(config, "project")
            hpsync.save_config(config, path)
            with (
                mock.patch.object(hpsync, "bootstrap_config", return_value=True) as bootstrap,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    hpsync.main(
                        ["--config", str(path), "config", "bootstrap", "project", "--yes"]
                    ),
                    0,
                )
            bootstrap.assert_called_once_with(config, ["project"], True)

    def test_prompt_helpers_handle_defaults_retries_and_yes_no(self) -> None:
        with (
            mock.patch("builtins.input", side_effect=["", "answer", "", ""]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(hpsync.prompt("Required"), "answer")
            self.assertEqual(hpsync.prompt("Defaulted", "default"), "default")
            self.assertEqual(hpsync.prompt("Optional", required=False), "")

        with (
            mock.patch("builtins.input", side_effect=["", "yes", "no"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(hpsync.prompt_yes_no("Default yes", default=True))
            self.assertTrue(hpsync.prompt_yes_no("Explicit yes"))
            self.assertFalse(hpsync.prompt_yes_no("Explicit no"))

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
