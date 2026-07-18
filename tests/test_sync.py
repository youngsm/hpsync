from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hpsync import cli as hpsync


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def endpoint(name: str, state: hpsync.FileState, dirty: bool) -> hpsync.Endpoint:
    location = hpsync.Location(name, f"/{name}", "local")
    return hpsync.Endpoint(
        location=location,
        root=f"/{name}",
        head="abc",
        branch="main",
        origin="",
        dirty={"file.txt": " M"} if dirty else {},
        states={"file.txt": state},
        operations=[],
    )


class PlanTests(unittest.TestCase):
    def test_one_change_propagates_to_every_different_location(self) -> None:
        old = hpsync.FileState("file", "old")
        new = hpsync.FileState("file", "new")
        plan = hpsync.make_plan(
            [endpoint("laptop", new, True), endpoint("office", old, False), endpoint("cluster", old, False)],
            set(),
            set(),
        )

        self.assertEqual(
            plan.updates,
            [
                hpsync.Update("file.txt", "laptop", "office"),
                hpsync.Update("file.txt", "laptop", "cluster"),
            ],
        )
        self.assertFalse(plan.conflicts)

    def test_different_changes_are_a_conflict(self) -> None:
        plan = hpsync.make_plan(
            [
                endpoint("laptop", hpsync.FileState("file", "one"), True),
                endpoint("office", hpsync.FileState("file", "two"), True),
                endpoint("cluster", hpsync.FileState("file", "old"), False),
            ],
            set(),
            set(),
        )

        self.assertEqual(plan.conflicts, ["file.txt"])
        self.assertFalse(plan.updates)


class SshCommandTests(unittest.TestCase):
    def test_uses_configured_host_port_and_control_socket(self) -> None:
        location = hpsync.Location(
            "cluster",
            "/work/project",
            "ssh",
            host="user@example.com",
            port=2222,
        )
        connection = hpsync.SshConnection(location)
        try:
            command = connection.command(["python3", "-c", "print('ok')"])
        finally:
            connection.tempdir.cleanup()

        self.assertEqual(command[0], "ssh")
        self.assertIn("2222", command)
        self.assertIn("user@example.com", command)
        self.assertIn("python3 -c", command[-1])

    def test_deletes_remote_paths_in_one_command(self) -> None:
        target = hpsync.Endpoint(
            hpsync.Location(
                "cluster",
                "/work/project",
                "ssh",
                host="user@example.com",
            ),
            "/work/project",
            "abc",
            "main",
            "",
            {},
            {},
            [],
        )
        connection = mock.Mock()

        hpsync.delete_paths(
            ["deleted-one.txt", "nested/deleted-two.txt"],
            target,
            {"cluster": connection},
        )

        connection.run.assert_called_once()
        self.assertEqual(
            connection.run.call_args.kwargs["input_data"],
            b'["deleted-one.txt", "nested/deleted-two.txt"]',
        )


class LocalSyncTests(unittest.TestCase):
    def test_copies_mixed_paths_in_one_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            target_root = root / "target"
            (source_root / "nested").mkdir(parents=True)
            target_root.mkdir()
            (source_root / "nested" / "file.txt").write_text("nested\n")
            (source_root / "with space.txt").write_text("space\n")
            executable = source_root / "-executable"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            (source_root / "link").symlink_to("nested/file.txt")
            (target_root / "deleted.txt").write_text("remove me\n")
            paths = [
                "nested/file.txt",
                "with space.txt",
                "-executable",
                "link",
                "deleted.txt",
            ]
            states = {
                path: hpsync.file_state(str(source_root), path) for path in paths
            }
            source = hpsync.Endpoint(
                hpsync.Location("source", str(source_root), "local"),
                str(source_root),
                "abc",
                "main",
                "",
                {},
                states,
                [],
            )
            target = hpsync.Endpoint(
                hpsync.Location("target", str(target_root), "local"),
                str(target_root),
                "abc",
                "main",
                "",
                {},
                {},
                [],
            )

            hpsync.copy_paths(paths, source, target, {})

            self.assertEqual((target_root / "nested" / "file.txt").read_text(), "nested\n")
            self.assertEqual((target_root / "with space.txt").read_text(), "space\n")
            self.assertTrue(os.access(target_root / "-executable", os.X_OK))
            self.assertTrue((target_root / "link").is_symlink())
            self.assertEqual(os.readlink(target_root / "link"), "nested/file.txt")
            self.assertFalse((target_root / "deleted.txt").exists())

    def test_apply_plan_groups_updates_by_source_and_target(self) -> None:
        endpoints = [
            hpsync.Endpoint(
                hpsync.Location(name, f"/{name}", "local"),
                f"/{name}",
                "abc",
                "main",
                "",
                {},
                {},
                [],
            )
            for name in ("source", "other", "target")
        ]
        plan = hpsync.SyncPlan(
            updates=[
                hpsync.Update("one.txt", "source", "target"),
                hpsync.Update("two.txt", "source", "target"),
                hpsync.Update("three.txt", "other", "target"),
            ],
            conflicts=[],
            converged=[],
            excluded=[],
        )

        with (
            mock.patch.object(hpsync, "create_backups", return_value={}),
            mock.patch.object(hpsync, "copy_paths") as copy_paths,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hpsync.apply_plan("project", plan, endpoints, {})

        self.assertEqual(copy_paths.call_count, 2)
        self.assertEqual(copy_paths.call_args_list[0].args[0], ["one.txt", "two.txt"])
        self.assertEqual(copy_paths.call_args_list[1].args[0], ["three.txt"])

    def test_bootstrap_creates_a_missing_local_checkout_without_an_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin.git"
            subprocess.run(
                ["git", "init", "--bare", "-q", "-b", "main", str(origin)],
                check=True,
                capture_output=True,
            )
            source = root / "source"
            subprocess.run(
                ["git", "clone", "-q", str(origin), str(source)],
                check=True,
                capture_output=True,
            )
            git(source, "config", "user.name", "Test User")
            git(source, "config", "user.email", "test@example.com")
            (source / "file.txt").write_text("base\n", encoding="utf-8")
            git(source, "add", "file.txt")
            git(source, "commit", "-qm", "base")
            git(source, "push", "-q", "origin", "HEAD")
            git(source, "remote", "remove", "origin")

            missing = root / "missing"
            config = hpsync.default_config()
            hpsync.add_repo(config, "project")
            hpsync.add_location(
                config,
                "project",
                {"name": "local", "transport": "local", "path": str(missing)},
            )
            hpsync.add_location(
                config,
                "project",
                {"name": "source", "transport": "local", "path": str(source)},
            )

            with contextlib.redirect_stdout(io.StringIO()):
                ready = hpsync.bootstrap_repository(
                    config["repositories"][0], assume_yes=True
                )

            self.assertTrue(ready)
            local_identity = hpsync.local_repo_identity(
                hpsync.location_from_dict(config["repositories"][0]["locations"][0])
            )
            source_identity = hpsync.local_repo_identity(
                hpsync.location_from_dict(config["repositories"][0]["locations"][1])
            )
            assert local_identity is not None
            assert source_identity is not None
            self.assertEqual(
                local_identity.head,
                source_identity.head,
            )
            self.assertEqual(local_identity.origin, "")
            self.assertEqual((missing / "file.txt").read_text(encoding="utf-8"), "base\n")

    def test_syncs_three_local_worktrees_and_creates_backups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            git(source, "init", "-q")
            git(source, "config", "user.name", "Test User")
            git(source, "config", "user.email", "test@example.com")
            (source / "file.txt").write_text("base\n", encoding="utf-8")
            git(source, "add", "file.txt")
            git(source, "commit", "-qm", "base")

            worktrees = []
            for name in ("laptop", "office", "cluster"):
                worktree = root / name
                subprocess.run(
                    ["git", "clone", "-q", str(source), str(worktree)],
                    check=True,
                    capture_output=True,
                )
                worktrees.append(worktree)

            (worktrees[0] / "file.txt").write_text("changed on laptop\n", encoding="utf-8")
            config = hpsync.default_config()
            hpsync.add_repo(config, "project")
            for name, worktree in zip(("laptop", "office", "cluster"), worktrees):
                hpsync.add_location(
                    config,
                    "project",
                    {
                        "name": name,
                        "transport": "local",
                        "path": str(worktree),
                        "state": str(root / f"state-{name}"),
                    },
                )
            config_path = root / "config.json"
            hpsync.save_config(config, config_path)

            with contextlib.redirect_stdout(io.StringIO()):
                result = hpsync.main(["--config", str(config_path), "sync", "--yes"])

            self.assertEqual(result, 0)
            for worktree in worktrees:
                self.assertEqual(
                    (worktree / "file.txt").read_text(encoding="utf-8"),
                    "changed on laptop\n",
                )
            self.assertTrue(list((root / "state-office" / "backups").glob("*.tar.gz")))
            self.assertTrue(list((root / "state-cluster" / "backups").glob("*.tar.gz")))


if __name__ == "__main__":
    unittest.main()
