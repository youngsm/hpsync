from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hpsync import cli as hpsync


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def endpoint(
    name: str,
    state: hpsync.FileState,
    dirty: bool,
    *,
    path: str = "file.txt",
    head: str = "abc",
    operations: list[str] | None = None,
    code: str = " M",
) -> hpsync.Endpoint:
    location = hpsync.Location(name, f"/{name}", "local")
    return hpsync.Endpoint(
        location=location,
        root=f"/{name}",
        head=head,
        branch="main",
        origin="",
        dirty={path: code} if dirty else {},
        states={path: state},
        operations=operations or [],
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

    def test_matching_excluded_and_unsupported_paths_are_classified(self) -> None:
        same = hpsync.FileState("file", "same")
        matching = hpsync.make_plan(
            [endpoint("one", same, True), endpoint("two", same, False)], set(), set()
        )
        self.assertEqual(matching.converged, ["file.txt"])

        excluded = hpsync.make_plan(
            [
                endpoint("one", hpsync.FileState("file", "new"), True, path=".env"),
                endpoint("two", hpsync.FileState("file", "old"), False, path=".env"),
            ],
            set(),
            {".env"},
        )
        self.assertEqual(excluded.excluded, [".env"])

        unsupported = hpsync.make_plan(
            [
                endpoint("one", hpsync.FileState("unsupported"), True),
                endpoint("two", hpsync.FileState("missing"), False),
            ],
            set(),
            set(),
        )
        self.assertEqual(unsupported.conflicts, ["file.txt"])

    def test_status_counts_distinguishes_git_change_kinds(self) -> None:
        item = endpoint("local", hpsync.FileState("file", "new"), False)
        item.dirty = {
            "modified": " M",
            "deleted": " D",
            "renamed": "R ",
            "copied": "C ",
            "new": "??",
        }

        self.assertEqual(
            hpsync.status_counts(item),
            {"modified": 1, "deleted": 1, "renamed": 2, "untracked": 1},
        )

    def test_preconditions_reject_git_operations_and_different_heads(self) -> None:
        clean = hpsync.FileState("file", "same")
        with self.assertRaisesRegex(RuntimeError, "active Git operations"):
            hpsync.ensure_preconditions(
                [
                    endpoint("one", clean, False, operations=["rebase"]),
                    endpoint("two", clean, False),
                ]
            )
        with self.assertRaisesRegex(RuntimeError, "bases differ"):
            hpsync.ensure_preconditions(
                [
                    endpoint("one", clean, False, head="abc"),
                    endpoint("two", clean, False, head="def"),
                ]
            )

        hpsync.ensure_preconditions(
            [endpoint("one", clean, False), endpoint("two", clean, False)]
        )


class PathAndProbeTests(unittest.TestCase):
    def test_parses_porcelain_renames_and_rejects_bad_records(self) -> None:
        raw = b" M changed.txt\0R  renamed.txt\0old.txt\0?? untracked.txt\0"
        self.assertEqual(
            hpsync.parse_porcelain(raw),
            {
                "changed.txt": " M",
                "renamed.txt": "R ",
                "old.txt": "R ",
                "untracked.txt": "??",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "Unexpected output"):
            hpsync.parse_porcelain(b"bad\0")
        with self.assertRaisesRegex(RuntimeError, "Incomplete rename"):
            hpsync.parse_porcelain(b"R  renamed.txt\0")

    def test_repository_paths_are_validated_and_excluded_safely(self) -> None:
        for path in ("", "/absolute", "../outside", "nested/../outside", "line\nbreak"):
            with self.subTest(path), self.assertRaises(RuntimeError):
                hpsync.validate_path(path)

        hpsync.validate_path("nested/file.txt")
        self.assertTrue(hpsync.excluded_path(".git/index", {".git"}, set()))
        self.assertTrue(hpsync.excluded_path("nested/.env", set(), {".env"}))
        self.assertFalse(hpsync.excluded_path("nested/file.txt", {".git"}, {".env"}))

    def test_file_state_tracks_missing_files_symlinks_and_executability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "script.sh"
            regular.write_text("#!/bin/sh\n", encoding="utf-8")
            regular.chmod(0o755)
            (root / "link").symlink_to("script.sh")
            (root / "folder").mkdir()

            regular_state = hpsync.file_state(str(root), "script.sh")
            self.assertEqual(regular_state.kind, "file")
            self.assertEqual(
                regular_state.digest, hashlib.sha256(b"#!/bin/sh\n").hexdigest()
            )
            self.assertTrue(regular_state.executable)
            self.assertEqual(hpsync.file_state(str(root), "link").kind, "symlink")
            self.assertEqual(hpsync.file_state(str(root), "folder").kind, "unsupported")
            self.assertEqual(hpsync.file_state(str(root), "missing").kind, "missing")

    def test_operation_markers_reports_in_progress_git_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            git_dir = Path(directory)
            (git_dir / "MERGE_HEAD").touch()
            (git_dir / "rebase-merge").mkdir()
            with mock.patch.object(
                hpsync, "git", return_value=os.fsencode(str(git_dir) + "\n")
            ):
                self.assertEqual(hpsync.operation_markers("/repo"), ["merge", "rebase"])

    def test_remote_probe_decodes_status_and_surfaces_ssh_errors(self) -> None:
        location = hpsync.Location(
            "cluster", "/work/project", "ssh", host="cluster.example.org"
        )
        payload = {
            "root": "/work/project",
            "head": "abc",
            "branch": "main",
            "origin": "git@example.org:project.git",
            "dirty": {"file.txt": " M"},
            "states": {
                "file.txt": {"kind": "file", "digest": "digest", "executable": False}
            },
            "operations": [],
        }
        connection = mock.Mock()
        connection.run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=b"banner\n" + json.dumps(payload).encode() + b"\n"
        )

        result = hpsync.probe_remote(location, connection, {"other.txt"})

        self.assertEqual(result.states["file.txt"], hpsync.FileState("file", "digest"))
        encoded_paths = connection.run.call_args.args[0][-1]
        self.assertEqual(json.loads(base64.b64decode(encoded_paths)), ["other.txt"])

        connection.run.return_value = subprocess.CompletedProcess([], 0, stdout=b"")
        with self.assertRaisesRegex(RuntimeError, "no repository status"):
            hpsync.probe_remote(location, connection)

        connection.run.side_effect = subprocess.CalledProcessError(
            1, ["ssh"], stderr=b"permission denied"
        )
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            hpsync.probe_remote(location, connection)

    def test_inspection_reprobes_locations_missing_cross_site_paths(self) -> None:
        local = hpsync.Location("local", "/local", "local")
        remote = hpsync.Location("remote", "/remote", "ssh", host="example.org")
        changed = endpoint("local", hpsync.FileState("file", "new"), True)
        first_remote = endpoint("remote", hpsync.FileState("missing"), False)
        first_remote.location = remote
        first_remote.states = {}
        complete_remote = endpoint("remote", hpsync.FileState("file", "old"), False)
        complete_remote.location = remote
        connection = mock.Mock()

        with (
            mock.patch.object(hpsync, "probe_local", return_value=changed) as probe_local,
            mock.patch.object(
                hpsync, "probe_remote", side_effect=[first_remote, complete_remote]
            ) as probe_remote,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            results = hpsync.inspect_locations([local, remote], {"remote": connection})

        self.assertEqual(results, [changed, complete_remote])
        probe_local.assert_called_once_with(local)
        self.assertEqual(probe_remote.call_args_list[1].args, (remote, connection, {"file.txt"}))


class SshCommandTests(unittest.TestCase):
    def test_requires_a_host_and_delegates_commands_to_the_runner(self) -> None:
        with self.assertRaisesRegex(hpsync.ConfigError, "has no host"):
            hpsync.SshConnection(hpsync.Location("cluster", "/work", "ssh"))

        location = hpsync.Location("cluster", "/work", "ssh", host="example.org")
        connection = hpsync.SshConnection(location)
        completed = subprocess.CompletedProcess([], 0, stdout=b"ok")
        try:
            with mock.patch.object(hpsync, "run", return_value=completed) as runner:
                self.assertIs(
                    connection.run(["printf", "ok"], capture_output=True), completed
                )
            runner.assert_called_once_with(
                connection.command(["printf", "ok"]),
                check=True,
                capture_output=True,
                input_data=None,
            )
        finally:
            with mock.patch.object(hpsync.subprocess, "run") as close:
                connection.close()
            self.assertIn("exit", close.call_args.args[0])

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

    def test_authentication_refreshes_only_when_the_probe_fails(self) -> None:
        location = hpsync.Location(
            "cluster",
            "/work",
            "ssh",
            host="example.org",
            port=2222,
            auth_command=("kinit", "-R"),
        )
        completed = lambda code: subprocess.CompletedProcess([], code)  # noqa: E731

        with mock.patch.object(hpsync.subprocess, "run", return_value=completed(0)) as runner:
            hpsync.ensure_authentication(location)
        runner.assert_called_once()

        with (
            mock.patch.object(
                hpsync.subprocess,
                "run",
                side_effect=[completed(1), completed(0), completed(0)],
            ) as runner,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hpsync.ensure_authentication(location)
        self.assertEqual(runner.call_args_list[1].args[0], ["kinit", "-R"])
        self.assertIn("2222", runner.call_args_list[0].args[0])

        with (
            mock.patch.object(
                hpsync.subprocess,
                "run",
                side_effect=[completed(1), completed(0), completed(1)],
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "still fails"),
        ):
            hpsync.ensure_authentication(location)

        without_refresh = hpsync.Location("cluster", "/work", "ssh", host="example.org")
        with mock.patch.object(hpsync.subprocess, "run") as runner:
            hpsync.ensure_authentication(without_refresh)
        runner.assert_not_called()

    def test_remote_identity_accepts_json_and_rejects_empty_output(self) -> None:
        location = hpsync.Location("cluster", "/work", "ssh", host="example.org")
        connection = mock.Mock()
        identity = {
            "root": "/work",
            "head": "abc",
            "branch": "main",
            "origin": "git@example.org:project.git",
        }
        connection.run.side_effect = [
            subprocess.CompletedProcess(
                [], 0, stdout=b"banner\n" + json.dumps(identity).encode() + b"\n"
            ),
            subprocess.CompletedProcess([], 0, stdout=b"null\n"),
            subprocess.CompletedProcess([], 0, stdout=b""),
        ]

        self.assertEqual(
            hpsync.remote_repo_identity(location, connection),
            hpsync.RepoIdentity(**identity),
        )
        self.assertIsNone(hpsync.remote_repo_identity(location, connection))
        with self.assertRaisesRegex(RuntimeError, "no repository information"):
            hpsync.remote_repo_identity(location, connection)


class LocalSyncTests(unittest.TestCase):
    def test_backups_archive_local_targets_and_request_one_remote_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_root = root / "local"
            local_root.mkdir()
            (local_root / "file.txt").write_text("before\n", encoding="utf-8")
            local = hpsync.Endpoint(
                hpsync.Location(
                    "local",
                    str(local_root),
                    "local",
                    state=str(root / "local-state"),
                ),
                str(local_root),
                "abc",
                "main",
                "",
                {},
                {},
                [],
            )
            remote = hpsync.Endpoint(
                hpsync.Location(
                    "remote",
                    "/work/project",
                    "ssh",
                    host="example.org",
                    state="/scratch/hpsync",
                ),
                "/work/project",
                "abc",
                "main",
                "",
                {},
                {},
                [],
            )
            plan = hpsync.SyncPlan(
                updates=[
                    hpsync.Update("file.txt", "source", "local"),
                    hpsync.Update("missing.txt", "source", "local"),
                    hpsync.Update("file.txt", "source", "remote"),
                ],
                conflicts=[],
                converged=[],
                excluded=[],
            )
            connection = mock.Mock()

            backups = hpsync.create_backups(
                "project/name",
                plan,
                {"local": local, "remote": remote},
                {"remote": connection},
            )

            with tarfile.open(backups["local"], "r:gz") as archive:
                self.assertEqual(archive.getnames(), ["file.txt"])
            self.assertIn("project-name-local", backups["local"])
            self.assertTrue(backups["remote"].startswith("/scratch/hpsync/backups/"))
            connection.run.assert_called_once()

    def test_local_removal_handles_files_directories_and_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file").write_text("data", encoding="utf-8")
            (root / "folder").mkdir()
            hpsync.remove_local(str(root), "file")
            hpsync.remove_local(str(root), "folder")
            hpsync.remove_local(str(root), "already-missing")
            self.assertFalse((root / "file").exists())
            self.assertFalse((root / "folder").exists())

    def test_repository_exclusions_use_defaults_or_overrides(self) -> None:
        self.assertEqual(
            hpsync.repo_exclusions({}),
            (hpsync.DEFAULT_EXCLUDED_PARTS, hpsync.DEFAULT_EXCLUDED_NAMES),
        )
        self.assertEqual(
            hpsync.repo_exclusions(
                {"exclude_parts": ["outputs"], "exclude_names": ["secret.txt"]}
            ),
            ({"outputs"}, {"secret.txt"}),
        )
        self.assertEqual(hpsync.safe_filename("repo/name @ host"), "repo-name---host")

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


class RepositoryFlowTests(unittest.TestCase):
    @staticmethod
    def repo() -> dict[str, object]:
        return {
            "name": "project",
            "locations": [
                {"name": "one", "transport": "local", "path": "/one"},
                {"name": "two", "transport": "local", "path": "/two"},
            ],
        }

    @staticmethod
    def args(command: str, *, yes: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            command=command,
            locations=[],
            verbose=False,
            yes=yes,
        )

    def test_status_blocks_different_commits_and_describes_each_plan_state(self) -> None:
        state = hpsync.FileState("file", "same")
        different_heads = [
            endpoint("one", state, False, head="abc"),
            endpoint("two", state, False, head="def"),
        ]
        with (
            mock.patch.object(hpsync, "inspect_locations", return_value=different_heads),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(hpsync.run_repository(self.repo(), self.args("status")), 2)

        cases = [
            (
                [
                    endpoint("one", hpsync.FileState("file", "one"), True),
                    endpoint("two", hpsync.FileState("file", "two"), True),
                ],
                "Resolve conflicts",
            ),
            (
                [
                    endpoint("one", hpsync.FileState("file", "new"), True),
                    endpoint("two", hpsync.FileState("file", "old"), False),
                ],
                "Ready to sync 1 updates",
            ),
            (
                [endpoint("one", state, True), endpoint("two", state, False)],
                "already match",
            ),
        ]
        for endpoints, message in cases:
            with self.subTest(message):
                output = io.StringIO()
                with (
                    mock.patch.object(hpsync, "inspect_locations", return_value=endpoints),
                    contextlib.redirect_stdout(output),
                ):
                    result = hpsync.run_repository(self.repo(), self.args("status"))
                self.assertEqual(result, 0)
                self.assertIn(message, output.getvalue())

    def test_sync_rejects_conflicts_and_returns_early_when_already_matching(self) -> None:
        conflict = [
            endpoint("one", hpsync.FileState("file", "one"), True),
            endpoint("two", hpsync.FileState("file", "two"), True),
        ]
        with (
            mock.patch.object(hpsync, "inspect_locations", return_value=conflict),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "conflicting paths"),
        ):
            hpsync.run_repository(self.repo(), self.args("sync", yes=True))

        same = hpsync.FileState("file", "same")
        matching = [endpoint("one", same, True), endpoint("two", same, False)]
        with (
            mock.patch.object(hpsync, "inspect_locations", return_value=matching),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                hpsync.run_repository(self.repo(), self.args("sync", yes=True)), 0
            )

    def test_sync_confirmation_can_cancel_or_detect_a_late_change(self) -> None:
        initial = [
            endpoint("one", hpsync.FileState("file", "new"), True),
            endpoint("two", hpsync.FileState("file", "old"), False),
        ]
        with (
            mock.patch.object(hpsync, "inspect_locations", return_value=initial) as inspect,
            mock.patch("builtins.input", return_value="no"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(hpsync.run_repository(self.repo(), self.args("sync")), 0)
        inspect.assert_called_once()

        with (
            mock.patch.object(hpsync, "inspect_locations", side_effect=[initial, []]),
            mock.patch("builtins.input", return_value="sync"),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "changed while the plan"),
        ):
            hpsync.run_repository(self.repo(), self.args("sync"))

    def test_sync_applies_and_verifies_the_reviewed_plan(self) -> None:
        new = hpsync.FileState("file", "new")
        initial = [endpoint("one", new, True), endpoint("two", hpsync.FileState("file", "old"), False)]
        final = [endpoint("one", new, True), endpoint("two", new, False)]
        with (
            mock.patch.object(
                hpsync, "inspect_locations", side_effect=[initial, initial, final]
            ),
            mock.patch.object(hpsync, "apply_plan") as apply_plan,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = hpsync.run_repository(self.repo(), self.args("sync", yes=True))

        self.assertEqual(result, 0)
        apply_plan.assert_called_once()

        with (
            mock.patch.object(
                hpsync, "inspect_locations", side_effect=[initial, initial, initial]
            ),
            mock.patch.object(hpsync, "apply_plan"),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "verification failed"),
        ):
            hpsync.run_repository(self.repo(), self.args("sync", yes=True))

    def test_repository_selection_and_sync_lock_fail_cleanly(self) -> None:
        with self.assertRaisesRegex(hpsync.ConfigError, "at least two"):
            hpsync.run_repository(
                {"name": "project", "locations": []}, self.args("status")
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            hpsync.save_config(hpsync.default_config(), config_path)
            args = argparse.Namespace(
                config_file=str(config_path),
                repositories=[],
                command="status",
            )
            with self.assertRaisesRegex(hpsync.ConfigError, "No repositories"):
                hpsync.cmd_worktrees(args)

            with hpsync.sync_lock(config_path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with hpsync.sync_lock(config_path):
                        pass


if __name__ == "__main__":
    unittest.main()
