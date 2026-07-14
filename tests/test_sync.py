from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

import hpsync


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


class LocalSyncTests(unittest.TestCase):
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
