#!/usr/bin/env python3
"""Safely synchronize uncommitted work across local and SSH Git worktrees."""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


APP_NAME = "hpsync"
CONFIG_ENV_VAR = "HPSYNC_CONFIG"
CONFIG_VERSION = 1
DEFAULT_STATE_PATH = "~/.local/state/hpsync"
DEFAULT_EXCLUDED_PARTS = {".git", ".venv", "__pycache__"}
DEFAULT_EXCLUDED_NAMES = {".env"}
SSH_QUIET = ["-q", "-o", "LogLevel=ERROR", "-o", "ForwardX11=no"]

_COLOR = False


def configure_color(mode: str) -> None:
    global _COLOR
    if mode == "never" or (mode == "auto" and os.environ.get("NO_COLOR") is not None):
        _COLOR = False
    elif mode == "always" or os.environ.get("CLICOLOR_FORCE") not in (None, "0"):
        _COLOR = True
    else:
        _COLOR = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def styled(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def progress(message: str) -> None:
    print(f"{styled('●', '36')} {message}", flush=True)


def success(message: str) -> None:
    print(f"{styled('✓', '32')} {message}", flush=True)


def warning(message: str) -> None:
    print(f"{styled('!', '33')} {message}", flush=True)


def title(message: str) -> str:
    return styled(message, "1;36")


class ConfigError(RuntimeError):
    """Raised when the hpsync configuration is missing or invalid."""


@dataclasses.dataclass(frozen=True)
class FileState:
    kind: str
    digest: str = ""
    executable: bool = False


@dataclasses.dataclass(frozen=True)
class Location:
    name: str
    path: str
    transport: str
    host: str | None = None
    port: int | None = None
    state: str = DEFAULT_STATE_PATH
    python: str = "python3"
    auth_command: tuple[str, ...] = ()

    @property
    def is_local(self) -> bool:
        return self.transport == "local"


@dataclasses.dataclass
class Endpoint:
    location: Location
    root: str
    head: str
    branch: str
    origin: str
    dirty: dict[str, str]
    states: dict[str, FileState]
    operations: list[str]

    @property
    def name(self) -> str:
        return self.location.name


@dataclasses.dataclass(frozen=True)
class Update:
    path: str
    source: str
    target: str


@dataclasses.dataclass
class SyncPlan:
    updates: list[Update]
    conflicts: list[str]
    converged: list[str]
    excluded: list[str]


@dataclasses.dataclass(frozen=True)
class RepoIdentity:
    root: str
    head: str
    branch: str
    origin: str


def default_config() -> dict[str, Any]:
    return {"version": CONFIG_VERSION, "repositories": []}


def get_config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if override := os.environ.get(CONFIG_ENV_VAR):
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / APP_NAME / "config.json"


def validate_string_list(value: Any, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigError(f"{field} must be a list of non-empty strings")


def validate_location(location: Any, repo_name: str = "") -> None:
    prefix = f"Repository {repo_name} location" if repo_name else "Location"
    if not isinstance(location, dict):
        raise ConfigError(f"{prefix} must be an object")
    for field in ("name", "path", "transport"):
        if not isinstance(location.get(field), str) or not location[field].strip():
            raise ConfigError(f"{prefix} must have a non-empty {field!r}")
    if location["transport"] not in {"local", "ssh"}:
        raise ConfigError(f"Location {location['name']} transport must be local or ssh")
    if location["transport"] == "ssh":
        if not isinstance(location.get("host"), str) or not location["host"].strip():
            raise ConfigError(f"SSH location {location['name']} must have a host")
        port = location.get("port")
        if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
            raise ConfigError(f"Location {location['name']} port must be between 1 and 65535")
    if "state" in location and (
        not isinstance(location["state"], str) or not location["state"].strip()
    ):
        raise ConfigError(f"Location {location['name']} state must be a non-empty path")
    if "python" in location and (
        not isinstance(location["python"], str) or not location["python"].strip()
    ):
        raise ConfigError(f"Location {location['name']} python must be non-empty")
    if "auth_command" in location:
        validate_string_list(location["auth_command"], f"Location {location['name']} auth_command")


def validate_config(config: Any) -> None:
    if not isinstance(config, dict):
        raise ConfigError("Config must be a JSON object")
    if config.get("version") != CONFIG_VERSION:
        raise ConfigError(
            f"Unsupported config version {config.get('version')!r}; expected {CONFIG_VERSION}"
        )
    repositories = config.get("repositories")
    if not isinstance(repositories, list):
        raise ConfigError("Config field 'repositories' must be a list")
    repo_names: set[str] = set()
    for repo in repositories:
        if not isinstance(repo, dict):
            raise ConfigError("Every repository must be an object")
        name = repo.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("Every repository must have a non-empty name")
        if name in repo_names:
            raise ConfigError(f"Duplicate repository name: {name}")
        repo_names.add(name)
        locations = repo.get("locations")
        if not isinstance(locations, list):
            raise ConfigError(f"Repository {name} locations must be a list")
        location_names: set[str] = set()
        for location in locations:
            validate_location(location, name)
            if location["name"] in location_names:
                raise ConfigError(f"Duplicate location {location['name']} in repository {name}")
            location_names.add(location["name"])
        for field in ("exclude_parts", "exclude_names"):
            if field in repo:
                validate_string_list(repo[field], f"Repository {name} {field}")


def load_config(path: Path | None = None, allow_missing: bool = False) -> dict[str, Any]:
    config_path = path or get_config_path()
    if not config_path.exists():
        if allow_missing:
            return default_config()
        raise ConfigError(f"No config at {config_path}; run 'hpsync config' to create one")
    try:
        with config_path.open(encoding="utf-8") as stream:
            config = json.load(stream)
    except json.JSONDecodeError as error:
        raise ConfigError(f"Invalid JSON in {config_path}: {error}") from error
    validate_config(config)
    return config


def save_config(config: dict[str, Any], path: Path | None = None) -> None:
    validate_config(config)
    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)


def find_repo(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((repo for repo in config["repositories"] if repo["name"] == name), None)


def require_repo(config: dict[str, Any], name: str) -> dict[str, Any]:
    repo = find_repo(config, name)
    if repo is None:
        raise ConfigError(f"Unknown repository: {name}")
    return repo


def add_repo(config: dict[str, Any], name: str) -> None:
    name = name.strip()
    if not name:
        raise ConfigError("Repository name cannot be blank")
    if find_repo(config, name):
        raise ConfigError(f"Repository already exists: {name}")
    config["repositories"].append(
        {
            "name": name,
            "locations": [],
            "exclude_parts": sorted(DEFAULT_EXCLUDED_PARTS),
            "exclude_names": sorted(DEFAULT_EXCLUDED_NAMES),
        }
    )


def remove_repo(config: dict[str, Any], name: str) -> None:
    before = len(config["repositories"])
    config["repositories"] = [repo for repo in config["repositories"] if repo["name"] != name]
    if len(config["repositories"]) == before:
        raise ConfigError(f"Unknown repository: {name}")


def add_location(
    config: dict[str, Any], repo_name: str, location: dict[str, Any], replace: bool = False
) -> None:
    validate_location(location, repo_name)
    repo = require_repo(config, repo_name)
    index = next(
        (index for index, item in enumerate(repo["locations"]) if item["name"] == location["name"]),
        None,
    )
    if index is not None and not replace:
        raise ConfigError(
            f"Location {location['name']} already exists in {repo_name}; use --replace"
        )
    if index is None:
        repo["locations"].append(location)
    else:
        repo["locations"][index] = location


def remove_location(config: dict[str, Any], repo_name: str, location_name: str) -> None:
    repo = require_repo(config, repo_name)
    before = len(repo["locations"])
    repo["locations"] = [item for item in repo["locations"] if item["name"] != location_name]
    if len(repo["locations"]) == before:
        raise ConfigError(f"Unknown location {location_name} in repository {repo_name}")


def location_from_dict(data: dict[str, Any]) -> Location:
    return Location(
        name=data["name"],
        path=data["path"],
        transport=data["transport"],
        host=data.get("host"),
        port=data.get("port"),
        state=data.get("state", DEFAULT_STATE_PATH),
        python=data.get("python", "python3"),
        auth_command=tuple(data.get("auth_command", [])),
    )


def select_repositories(
    config: dict[str, Any], requested: Iterable[str] | None
) -> list[dict[str, Any]]:
    names = list(requested or [])
    return [require_repo(config, name) for name in names] if names else config["repositories"]


def select_locations(repo: dict[str, Any], requested: Iterable[str] | None) -> list[Location]:
    locations = [location_from_dict(item) for item in repo["locations"]]
    names = list(requested or [])
    if not names:
        return locations
    by_name = {location.name: location for location in locations}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise ConfigError(f"Repository {repo['name']} has no location named {missing[0]}")
    return [by_name[name] for name in names]


def run(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        check=check,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        input=input_data,
    )


def git(repo: str, *args: str, check: bool = True) -> bytes:
    return run(["git", "-C", repo, *args], check=check, capture_output=True).stdout


def parse_porcelain(raw: bytes) -> dict[str, str]:
    records = raw.split(b"\0")
    dirty: dict[str, str] = {}
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise RuntimeError("Unexpected output from git status")
        code = os.fsdecode(record[:2])
        path = os.fsdecode(record[3:])
        dirty[path] = code
        if "R" in code or "C" in code:
            if index >= len(records) or not records[index]:
                raise RuntimeError("Incomplete rename record from git status")
            dirty[os.fsdecode(records[index])] = code
            index += 1
    return dirty


def validate_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
        raise RuntimeError(f"Unsafe repository path: {path!r}")
    if "\n" in path or "\r" in path:
        raise RuntimeError(f"Unsupported newline in repository path: {path!r}")


def excluded_path(path: str, excluded_parts: set[str], excluded_names: set[str]) -> bool:
    validate_path(path)
    parts = PurePosixPath(path).parts
    return bool(excluded_parts.intersection(parts) or Path(path).name in excluded_names)


def file_state(root: str, path: str) -> FileState:
    validate_path(path)
    absolute = Path(root) / path
    try:
        info = absolute.lstat()
    except FileNotFoundError:
        return FileState("missing")
    if stat.S_ISLNK(info.st_mode):
        return FileState("symlink", hashlib.sha256(os.fsencode(os.readlink(absolute))).hexdigest())
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with absolute.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return FileState("file", digest.hexdigest(), bool(info.st_mode & 0o111))
    return FileState("unsupported")


def operation_markers(repo: str) -> list[str]:
    git_dir = Path(os.fsdecode(git(repo, "rev-parse", "--absolute-git-dir")).strip())
    markers = {
        "merge": git_dir / "MERGE_HEAD",
        "cherry-pick": git_dir / "CHERRY_PICK_HEAD",
        "revert": git_dir / "REVERT_HEAD",
        "rebase": git_dir / "rebase-merge",
        "rebase-apply": git_dir / "rebase-apply",
        "bisect": git_dir / "BISECT_LOG",
    }
    return [name for name, marker in markers.items() if marker.exists()]


def probe_local(location: Location, extra_paths: set[str] | None = None) -> Endpoint:
    repo = str(Path(location.path).expanduser())
    root = os.fsdecode(git(repo, "rev-parse", "--show-toplevel")).strip()
    dirty = parse_porcelain(
        git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no")
    )
    branch = os.fsdecode(git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False)).strip()
    origin = os.fsdecode(git(repo, "remote", "get-url", "origin", check=False)).strip()
    paths = set(dirty).union(extra_paths or set())
    return Endpoint(
        location=location,
        root=root,
        head=os.fsdecode(git(repo, "rev-parse", "HEAD")).strip(),
        branch=branch or "DETACHED",
        origin=origin,
        dirty=dirty,
        states={path: file_state(root, path) for path in paths},
        operations=operation_markers(repo),
    )


REMOTE_PROBE = r"""
import base64, hashlib, json, os, stat, subprocess, sys
from pathlib import Path
repo = os.path.expanduser(sys.argv[1])
def git(*args, check=True):
    p = subprocess.run(["git", "-C", repo, *args], check=check,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.stdout
def parse(raw):
    records, dirty, i = raw.split(b"\0"), {}, 0
    while i < len(records):
        record, i = records[i], i + 1
        if not record:
            continue
        code, path = os.fsdecode(record[:2]), os.fsdecode(record[3:])
        dirty[path] = code
        if "R" in code or "C" in code:
            dirty[os.fsdecode(records[i])] = code
            i += 1
    return dirty
def state(root, path):
    absolute = Path(root) / path
    try:
        info = absolute.lstat()
    except FileNotFoundError:
        return {"kind": "missing", "digest": "", "executable": False}
    if stat.S_ISLNK(info.st_mode):
        digest = hashlib.sha256(os.fsencode(os.readlink(absolute))).hexdigest()
        return {"kind": "symlink", "digest": digest, "executable": False}
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with absolute.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"kind": "file", "digest": digest.hexdigest(),
                "executable": bool(info.st_mode & 0o111)}
    return {"kind": "unsupported", "digest": "", "executable": False}
root = os.fsdecode(git("rev-parse", "--show-toplevel")).strip()
dirty = parse(git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"))
branch = os.fsdecode(git("symbolic-ref", "--short", "-q", "HEAD", check=False)).strip()
origin = os.fsdecode(git("remote", "get-url", "origin", check=False)).strip()
git_dir = Path(os.fsdecode(git("rev-parse", "--absolute-git-dir")).strip())
markers = {"merge": "MERGE_HEAD", "cherry-pick": "CHERRY_PICK_HEAD",
           "revert": "REVERT_HEAD", "rebase": "rebase-merge",
           "rebase-apply": "rebase-apply", "bisect": "BISECT_LOG"}
extra = json.loads(base64.b64decode(sys.argv[2]))
paths = set(dirty).union(extra)
print(json.dumps({"root": root, "head": os.fsdecode(git("rev-parse", "HEAD")).strip(),
                  "branch": branch or "DETACHED", "origin": origin, "dirty": dirty,
                  "states": {path: state(root, path) for path in paths},
                  "operations": [name for name, marker in markers.items()
                                 if (git_dir / marker).exists()]}))
"""


class SshConnection:
    def __init__(self, location: Location):
        if not location.host:
            raise ConfigError(f"SSH location {location.name} has no host")
        self.location = location
        self.tempdir = tempfile.TemporaryDirectory(prefix="hpsync-")
        self.socket = str(Path(self.tempdir.name) / "ssh")

    def command(self, command: list[str]) -> list[str]:
        result = ["ssh", *SSH_QUIET, "-T"]
        if self.location.port:
            result.extend(["-p", str(self.location.port)])
        result.extend(
            [
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=120",
                "-o",
                f"ControlPath={self.socket}",
                self.location.host or "",
                shlex.join(command),
            ]
        )
        return result

    def run(
        self,
        command: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return run(
            self.command(command),
            check=check,
            capture_output=capture_output,
            input_data=input_data,
        )

    def close(self) -> None:
        close = ["ssh", *SSH_QUIET]
        if self.location.port:
            close.extend(["-p", str(self.location.port)])
        close.extend(["-S", self.socket, "-O", "exit", self.location.host or ""])
        subprocess.run(close, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.tempdir.cleanup()

    def __enter__(self) -> SshConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def ensure_authentication(location: Location) -> None:
    if not location.auth_command:
        return
    probe = ["ssh", *SSH_QUIET, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if location.port:
        probe.extend(["-p", str(location.port)])
    probe.extend([location.host or "", "true"])
    if subprocess.run(probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return
    warning(f"Authentication for {location.name} needs refreshing")
    subprocess.run(list(location.auth_command), check=True)
    if subprocess.run(probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise RuntimeError(f"Authentication for {location.name} still fails")


REMOTE_IDENTITY = r"""
import json, os, subprocess, sys
path = os.path.expanduser(sys.argv[1])
def git(*args):
    return subprocess.run(["git", "-C", path, *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
inside = git("rev-parse", "--is-inside-work-tree")
if inside.returncode:
    print(json.dumps(None))
else:
    root = git("rev-parse", "--show-toplevel").stdout.decode().strip()
    head = git("rev-parse", "HEAD").stdout.decode().strip()
    branch = git("symbolic-ref", "--short", "-q", "HEAD").stdout.decode().strip()
    origin = git("remote", "get-url", "origin").stdout.decode().strip()
    print(json.dumps({"root": root, "head": head,
                      "branch": branch or "DETACHED", "origin": origin}))
"""


REMOTE_CLONE_BUNDLE = r"""
import os, subprocess, sys, tempfile
from pathlib import Path
origin, head = sys.argv[1], sys.argv[2]
path = os.path.expanduser(sys.argv[3])
Path(path).parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(prefix="hpsync-", suffix=".bundle") as bundle:
    while True:
        chunk = sys.stdin.buffer.read(1024 * 1024)
        if not chunk:
            break
        bundle.write(chunk)
    bundle.flush()
    subprocess.run(["git", "clone", "--quiet", bundle.name, path], check=True)
if origin:
    subprocess.run(["git", "-C", path, "remote", "set-url", "origin", origin], check=True)
else:
    subprocess.run(["git", "-C", path, "remote", "remove", "origin"], check=True)
current = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], check=True,
                         stdout=subprocess.PIPE).stdout.decode().strip()
if current != head:
    subprocess.run(["git", "-C", path, "checkout", "--quiet", "--detach", head], check=True)
"""


def local_repo_identity(location: Location) -> RepoIdentity | None:
    path = str(Path(location.path).expanduser())
    inside = subprocess.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if inside.returncode:
        return None
    return RepoIdentity(
        root=os.fsdecode(git(path, "rev-parse", "--show-toplevel")).strip(),
        head=os.fsdecode(git(path, "rev-parse", "HEAD")).strip(),
        branch=os.fsdecode(
            git(path, "symbolic-ref", "--short", "-q", "HEAD", check=False)
        ).strip()
        or "DETACHED",
        origin=os.fsdecode(git(path, "remote", "get-url", "origin", check=False)).strip(),
    )


def remote_repo_identity(
    location: Location, connection: SshConnection
) -> RepoIdentity | None:
    result = connection.run(
        [location.python, "-c", REMOTE_IDENTITY, location.path],
        capture_output=True,
    )
    lines = result.stdout.splitlines()
    if not lines:
        raise RuntimeError(f"{location.name} returned no repository information")
    data = json.loads(lines[-1])
    return RepoIdentity(**data) if data else None


def location_repo_identity(
    location: Location, connections: dict[str, SshConnection]
) -> RepoIdentity | None:
    if location.is_local:
        return local_repo_identity(location)
    return remote_repo_identity(location, connections[location.name])


@contextlib.contextmanager
def repository_bundle(
    location: Location,
    source: RepoIdentity,
    connections: dict[str, SshConnection],
) -> Iterable[Path]:
    with tempfile.NamedTemporaryFile(prefix="hpsync-", suffix=".bundle") as bundle:
        command = ["git", "-C", source.root, "bundle", "create", "-", "--all"]
        if not location.is_local:
            command = connections[location.name].command(command)
        result = subprocess.run(command, stdout=bundle)
        if result.returncode:
            raise RuntimeError(f"Could not create a Git bundle from {location.name}")
        bundle.flush()
        yield Path(bundle.name)


def clone_bundle_to_location(
    location: Location,
    source: RepoIdentity,
    bundle: Path,
    connections: dict[str, SshConnection],
) -> None:
    progress(f"Creating repository at {location.name}:{location.path}")
    if location.is_local:
        path = Path(location.path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", str(bundle), str(path)], check=True)
        if source.origin:
            git(str(path), "remote", "set-url", "origin", source.origin)
        else:
            git(str(path), "remote", "remove", "origin")
        current = os.fsdecode(git(str(path), "rev-parse", "HEAD")).strip()
        if current != source.head:
            subprocess.run(
                ["git", "-C", str(path), "checkout", "--quiet", "--detach", source.head],
                check=True,
            )
    else:
        try:
            with bundle.open("rb") as stream:
                subprocess.run(
                    connections[location.name].command(
                        [
                            location.python,
                            "-c",
                            REMOTE_CLONE_BUNDLE,
                            source.origin,
                            source.head,
                            location.path,
                        ]
                    ),
                    check=True,
                    stdin=stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        except subprocess.CalledProcessError as error:
            detail = error.stderr.decode(errors="replace").strip() if error.stderr else ""
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Could not create {location.name}:{location.path}{suffix}") from error
    success(f"Created {location.name}:{location.path}")


def bootstrap_repository(repo: dict[str, Any], assume_yes: bool = False) -> bool:
    locations = select_locations(repo, None)
    if len(locations) < 2:
        raise ConfigError(f"Repository {repo['name']} needs a local copy and another location")
    with contextlib.ExitStack() as stack:
        connections: dict[str, SshConnection] = {}
        for location in locations:
            if not location.is_local:
                ensure_authentication(location)
                connections[location.name] = stack.enter_context(SshConnection(location))
        identities = {
            location.name: location_repo_identity(location, connections)
            for location in locations
        }
        existing = [location for location in locations if identities[location.name] is not None]
        missing = [location for location in locations if identities[location.name] is None]
        for location in existing:
            print(f"  found    {location.name}: {location.path}")
        for location in missing:
            print(f"  missing  {location.name}: {location.path}")
        if not existing:
            raise ConfigError(
                f"No configured location contains repository {repo['name']}; at least one copy must exist"
            )
        heads = {identities[location.name].head for location in existing if identities[location.name]}
        if len(heads) != 1:
            raise RuntimeError(
                f"Existing copies of {repo['name']} are on different commits; align them before setup"
            )
        if not missing:
            success(f"Every location already has {repo['name']}")
            return True
        source_location = existing[0]
        source = identities[source_location.name]
        assert source is not None
        if not assume_yes and not prompt_yes_no(
            f"Create {len(missing)} missing checkout(s) from {source_location.name}?",
            default=True,
        ):
            warning("Missing checkouts were not created")
            return False
        with repository_bundle(source_location, source, connections) as bundle:
            for location in missing:
                clone_bundle_to_location(location, source, bundle, connections)
        return True


def bootstrap_config(
    config: dict[str, Any], repositories: Iterable[str] | None = None, assume_yes: bool = False
) -> bool:
    ready = True
    for repo in select_repositories(config, repositories):
        print(f"\n{title('Checking ' + repo['name'])}")
        ready = bootstrap_repository(repo, assume_yes) and ready
    return ready


def probe_remote(
    location: Location,
    connection: SshConnection,
    extra_paths: set[str] | None = None,
) -> Endpoint:
    encoded = base64.b64encode(json.dumps(sorted(extra_paths or set())).encode()).decode()
    try:
        result = connection.run(
            [location.python, "-c", REMOTE_PROBE, location.path, encoded],
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace").strip() if error.stderr else ""
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Could not inspect {location.name} through {location.host}{suffix}") from error
    lines = result.stdout.splitlines()
    if not lines:
        raise RuntimeError(f"{location.name} returned no repository status")
    data = json.loads(lines[-1])
    return Endpoint(
        location=location,
        root=data["root"],
        head=data["head"],
        branch=data["branch"],
        origin=data["origin"],
        dirty=data["dirty"],
        states={path: FileState(**state) for path, state in data["states"].items()},
        operations=data["operations"],
    )


def inspect_locations(
    locations: list[Location], connections: dict[str, SshConnection]
) -> list[Endpoint]:
    first: list[Endpoint] = []
    for location in locations:
        progress(f"Inspecting {location.name}")
        if location.is_local:
            first.append(probe_local(location))
        else:
            first.append(probe_remote(location, connections[location.name]))
    paths = set().union(*(set(endpoint.dirty) for endpoint in first))
    endpoints: list[Endpoint] = []
    for endpoint in first:
        if paths.issubset(endpoint.states):
            endpoints.append(endpoint)
        elif endpoint.location.is_local:
            endpoints.append(probe_local(endpoint.location, paths))
        else:
            endpoints.append(
                probe_remote(endpoint.location, connections[endpoint.name], paths)
            )
    return endpoints


def make_plan(
    endpoints: list[Endpoint], excluded_parts: set[str], excluded_names: set[str]
) -> SyncPlan:
    updates: list[Update] = []
    conflicts: list[str] = []
    converged: list[str] = []
    excluded: list[str] = []
    paths = sorted(set().union(*(set(endpoint.dirty) for endpoint in endpoints)))
    for path in paths:
        if excluded_path(path, excluded_parts, excluded_names):
            excluded.append(path)
            continue
        states = {endpoint.states[path] for endpoint in endpoints}
        if len(states) == 1:
            converged.append(path)
            continue
        if any(state.kind == "unsupported" for state in states):
            conflicts.append(path)
            continue
        changed = [endpoint for endpoint in endpoints if path in endpoint.dirty]
        changed_states = {endpoint.states[path] for endpoint in changed}
        if len(changed_states) != 1:
            conflicts.append(path)
            continue
        source = changed[0]
        source_state = source.states[path]
        for target in endpoints:
            if target.states[path] != source_state:
                updates.append(Update(path, source.name, target.name))
    return SyncPlan(updates, conflicts, converged, excluded)


def status_counts(endpoint: Endpoint) -> dict[str, int]:
    counts = {"modified": 0, "deleted": 0, "renamed": 0, "untracked": 0}
    for code in endpoint.dirty.values():
        if code == "??":
            counts["untracked"] += 1
        elif "R" in code or "C" in code:
            counts["renamed"] += 1
        elif "D" in code:
            counts["deleted"] += 1
        else:
            counts["modified"] += 1
    return counts


def print_endpoint(
    endpoint: Endpoint,
    *,
    verbose: bool,
    excluded_parts: set[str],
    excluded_names: set[str],
) -> None:
    counts = status_counts(endpoint)
    count_text = ", ".join(f"{count} {name}" for name, count in counts.items() if count) or "clean"
    kind = "local" if endpoint.location.is_local else endpoint.location.host
    print(f"\n{styled(endpoint.name, '1')}  {endpoint.branch} @ {endpoint.head[:12]}  {styled(str(kind), '2')}")
    if endpoint.dirty:
        noun = "path" if len(endpoint.dirty) == 1 else "paths"
        print(f"  {len(endpoint.dirty)} changed {noun}  {styled('·', '2')}  {count_text}")
    else:
        print(f"  {styled('clean', '32')}")
    if verbose:
        print(f"  root    {endpoint.root}")
        print(f"  origin  {endpoint.origin or '<none>'}")
    if endpoint.operations:
        warning(f"{endpoint.name} is blocked by: {', '.join(endpoint.operations)}")
    if verbose:
        for path, code in sorted(endpoint.dirty.items()):
            suffix = " [excluded]" if excluded_path(path, excluded_parts, excluded_names) else ""
            print(f"    {code} {path}{suffix}")


def print_path_group(label: str, paths: list[str], verbose: bool) -> None:
    print(f"\n{styled(label, '1')}  {len(paths)}")
    shown = paths if verbose else paths[:10]
    for path in shown:
        print(f"  {path}")
    if len(shown) < len(paths):
        print(f"  ... and {len(paths) - len(shown)} more; rerun with --verbose")


def print_plan(plan: SyncPlan, verbose: bool) -> None:
    grouped: dict[tuple[str, str], list[str]] = {}
    for update in plan.updates:
        grouped.setdefault((update.source, update.target), []).append(update.path)
    print(f"\n{title('Plan')}")
    if grouped:
        for (source, target), paths in grouped.items():
            print(f"  {source} {styled('→', '36')} {target}  {len(paths)}")
    else:
        print("  updates          0")
    conflict_count = styled(str(len(plan.conflicts)), "31" if plan.conflicts else "32")
    print(f"  conflicts       {conflict_count}")
    print(f"  matching        {len(plan.converged)}")
    print(f"  excluded        {len(plan.excluded)}")
    if verbose:
        for (source, target), paths in grouped.items():
            print_path_group(f"Copy {source} → {target}", paths, True)
    if plan.conflicts:
        print_path_group("Conflicts", plan.conflicts, True)
        warning("Multiple locations changed these paths differently; nothing will be applied")
    if plan.converged and verbose:
        print_path_group("Already matching", plan.converged, True)
    if plan.excluded and verbose:
        print_path_group("Excluded paths", plan.excluded, True)


def ensure_preconditions(endpoints: list[Endpoint]) -> None:
    blocked = [endpoint.name for endpoint in endpoints if endpoint.operations]
    if blocked:
        raise RuntimeError(f"Finish active Git operations on: {', '.join(blocked)}")
    heads = {endpoint.head for endpoint in endpoints}
    if len(heads) != 1:
        detail = "\n".join(f"  {endpoint.name}: {endpoint.head}" for endpoint in endpoints)
        raise RuntimeError(f"Repository bases differ; synchronize committed history first:\n{detail}")


BACKUP_REMOTE = r"""
import base64, json, os, sys, tarfile
from pathlib import Path
root = Path(sys.argv[1])
destination = Path(os.path.expanduser(sys.argv[2]))
paths = json.loads(base64.b64decode(sys.argv[3]))
destination.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(destination, "w:gz", dereference=False) as archive:
    for path in paths:
        absolute = root / path
        if absolute.exists() or absolute.is_symlink():
            archive.add(absolute, arcname=path, recursive=False)
"""


DELETE_REMOTE = r"""
import json, sys
from pathlib import Path, PurePosixPath
root = Path(sys.argv[1])
for item in json.load(sys.stdin):
    parsed = PurePosixPath(item)
    if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
        raise RuntimeError(f"Unsafe repository path: {item!r}")
    path = root / item
    try:
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    except FileNotFoundError:
        pass
"""


def safe_filename(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "-" for character in value)


def create_local_backup(root: str, paths: list[str], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz", dereference=False) as archive:
        for path in paths:
            absolute = Path(root) / path
            if absolute.exists() or absolute.is_symlink():
                archive.add(absolute, arcname=path, recursive=False)


def create_backups(
    repo_name: str,
    plan: SyncPlan,
    endpoints: dict[str, Endpoint],
    connections: dict[str, SshConnection],
) -> dict[str, str]:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    targets: dict[str, list[str]] = {}
    for update in plan.updates:
        targets.setdefault(update.target, []).append(update.path)
    backups: dict[str, str] = {}
    for name, paths in targets.items():
        endpoint = endpoints[name]
        filename = f"{timestamp}-{safe_filename(repo_name)}-{safe_filename(name)}.tar.gz"
        destination = str(PurePosixPath(endpoint.location.state) / "backups" / filename)
        if endpoint.location.is_local:
            local_destination = Path(destination).expanduser()
            create_local_backup(endpoint.root, sorted(set(paths)), local_destination)
            backups[name] = str(local_destination)
        else:
            encoded = base64.b64encode(json.dumps(sorted(set(paths))).encode()).decode()
            connections[name].run(
                [endpoint.location.python, "-c", BACKUP_REMOTE, endpoint.root, destination, encoded]
            )
            backups[name] = destination
    return backups


def remove_local(root: str, path: str) -> None:
    absolute = Path(root) / path
    try:
        if absolute.is_dir() and not absolute.is_symlink():
            absolute.rmdir()
        else:
            absolute.unlink()
    except FileNotFoundError:
        pass


def endpoint_command(
    endpoint: Endpoint,
    command: list[str],
    connections: dict[str, SshConnection],
) -> list[str]:
    return command if endpoint.location.is_local else connections[endpoint.name].command(command)


def delete_paths(
    paths: list[str],
    target: Endpoint,
    connections: dict[str, SshConnection],
) -> None:
    if not paths:
        return
    for path in paths:
        validate_path(path)
    if target.location.is_local:
        for path in paths:
            remove_local(target.root, path)
        return
    connections[target.name].run(
        [target.location.python, "-c", DELETE_REMOTE, target.root],
        input_data=json.dumps(paths).encode(),
    )


def copy_paths(
    paths: list[str],
    source: Endpoint,
    target: Endpoint,
    connections: dict[str, SshConnection],
) -> None:
    unique_paths = sorted(set(paths))
    for path in unique_paths:
        validate_path(path)
    present = [path for path in unique_paths if source.states[path].kind != "missing"]
    missing = [path for path in unique_paths if source.states[path].kind == "missing"]

    if present:
        source_command = endpoint_command(
            source,
            ["tar", "-C", source.root, "-cf", "-", "--null", "-T", "-"],
            connections,
        )
        target_command = endpoint_command(
            target, ["tar", "-C", target.root, "-xf", "-"], connections
        )
        path_list = b"\0".join(os.fsencode(path) for path in present) + b"\0"
        source_process = subprocess.Popen(
            source_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        assert source_process.stdin is not None
        assert source_process.stdout is not None
        target_process = subprocess.Popen(target_command, stdin=source_process.stdout)
        source_process.stdout.close()
        try:
            source_process.stdin.write(path_list)
        except BrokenPipeError:
            pass
        finally:
            source_process.stdin.close()
        target_result = target_process.wait()
        source_result = source_process.wait()
        if source_result or target_result:
            raise RuntimeError(
                f"Failed to copy {len(present)} path(s) "
                f"from {source.name} to {target.name}"
            )

    delete_paths(missing, target, connections)


def copy_path(
    path: str,
    source: Endpoint,
    target: Endpoint,
    connections: dict[str, SshConnection],
) -> None:
    copy_paths([path], source, target, connections)


def apply_plan(
    repo_name: str,
    plan: SyncPlan,
    endpoints: list[Endpoint],
    connections: dict[str, SshConnection],
) -> None:
    by_name = {endpoint.name: endpoint for endpoint in endpoints}
    progress("Creating safety backups")
    backups = create_backups(repo_name, plan, by_name, connections)
    for name, destination in backups.items():
        print(f"  {name}  {destination}")
    grouped: dict[tuple[str, str], list[str]] = {}
    for update in plan.updates:
        grouped.setdefault((update.source, update.target), []).append(update.path)
    progress(
        f"Applying {len(plan.updates)} updates in {len(grouped)} transfer batch(es)"
    )
    for index, ((source, target), paths) in enumerate(grouped.items(), 1):
        print(
            f"  {styled(f'[{index}/{len(grouped)}]', '2')} "
            f"{source} {styled('→', '36')} {target}  {len(paths)} path(s)",
            flush=True,
        )
        copy_paths(paths, by_name[source], by_name[target], connections)


def repo_exclusions(repo: dict[str, Any]) -> tuple[set[str], set[str]]:
    return (
        set(repo.get("exclude_parts", DEFAULT_EXCLUDED_PARTS)),
        set(repo.get("exclude_names", DEFAULT_EXCLUDED_NAMES)),
    )


def run_repository(repo: dict[str, Any], args: argparse.Namespace) -> int:
    locations = select_locations(repo, args.locations)
    if len(locations) < 2:
        raise ConfigError(f"Repository {repo['name']} needs at least two selected locations")
    excluded_parts, excluded_names = repo_exclusions(repo)
    print(f"\n{title(repo['name'])}")
    with contextlib.ExitStack() as stack:
        connections: dict[str, SshConnection] = {}
        for location in locations:
            if not location.is_local:
                ensure_authentication(location)
                connections[location.name] = stack.enter_context(SshConnection(location))
        endpoints = inspect_locations(locations, connections)
        success("Inspection complete")
        for endpoint in endpoints:
            print_endpoint(
                endpoint,
                verbose=args.verbose,
                excluded_parts=excluded_parts,
                excluded_names=excluded_names,
            )
        if args.command == "status":
            if len({endpoint.head for endpoint in endpoints}) != 1:
                print(f"\n{styled('Synchronization blocked', '1;31')}")
                warning("Git commits differ; no files were changed")
                return 2
            plan = make_plan(endpoints, excluded_parts, excluded_names)
            print_plan(plan, args.verbose)
            if plan.conflicts:
                warning("Resolve conflicts before running hpsync sync")
            elif plan.updates:
                success(f"Ready to sync {len(plan.updates)} updates")
            else:
                success("Working trees already match")
            return 0

        ensure_preconditions(endpoints)
        plan = make_plan(endpoints, excluded_parts, excluded_names)
        print_plan(plan, True)
        if plan.conflicts:
            raise RuntimeError("Resolve conflicting paths before synchronizing")
        if not plan.updates:
            success("Working trees already match")
            return 0
        if not args.yes:
            response = input(
                f"\n{styled('Type sync', '1')} to apply {len(plan.updates)} updates: "
            )
            if response.strip().lower() != "sync":
                warning("No changes applied")
                return 0
        progress("Checking for changes since review")
        current = inspect_locations(locations, connections)
        if current != endpoints:
            raise RuntimeError("A working tree changed while the plan was being reviewed")
        apply_plan(repo["name"], plan, endpoints, connections)
        progress("Verifying all working trees")
        final = inspect_locations(locations, connections)
        final_plan = make_plan(final, excluded_parts, excluded_names)
        if final_plan.updates or final_plan.conflicts:
            raise RuntimeError("Post-synchronization verification failed")
        success("Synchronization complete and verified")
        return 0


@contextlib.contextmanager
def sync_lock(config_path: Path) -> Iterable[None]:
    lock_path = config_path.parent / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another hpsync process is already running") from error
        yield


def cmd_worktrees(args: argparse.Namespace) -> int:
    config_path = get_config_path(args.config_file)
    config = load_config(config_path)
    repositories = select_repositories(config, args.repositories)
    if not repositories:
        raise ConfigError("No repositories configured; run 'hpsync config'")
    result = 0
    manager = sync_lock(config_path) if args.command == "sync" else contextlib.nullcontext()
    with manager:
        for repo in repositories:
            result = max(result, run_repository(repo, args))
    return result


def question_separator() -> None:
    width = max(12, shutil.get_terminal_size(fallback=(72, 24)).columns - 1)
    print(styled("-" * width, "90"))


def prompt(label: str, default: str | None = None, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        question_separator()
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""


def prompt_yes_no(label: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    question_separator()
    value = input(f"{label} [{marker}]: ").strip().lower()
    return default if not value else value in {"y", "yes"}


class _RestartWizard(Exception):
    """Restart collection after discarding the most recent wizard answer."""


class WizardPrompts:
    """Interactive prompts that can replay prior answers after going back."""

    def __init__(self) -> None:
        self.answers: list[str] = []
        self.position = 0

    def explain(self, message: str) -> None:
        if self.position >= len(self.answers):
            print(message)

    def _ask(self, question: str) -> tuple[str, bool]:
        if self.position < len(self.answers):
            value = self.answers[self.position]
            self.position += 1
            return value, True

        while True:
            question_separator()
            value = input(question).strip()
            if value.lower() != "back":
                return value, False
            if not self.answers:
                warning("Already at the first question")
                continue
            self.answers.pop()
            self.position = 0
            print(f"{styled('↶', '36')} Returning to the previous question")
            raise _RestartWizard

    def _remember(self, value: str, replayed: bool) -> None:
        if not replayed:
            self.answers.append(value)
            self.position += 1

    def prompt(
        self, label: str, default: str | None = None, required: bool = True
    ) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            value, replayed = self._ask(f"{label}{suffix}: ")
            if replayed:
                return value
            if value:
                answer = value
            elif default is not None:
                answer = default
            elif not required:
                answer = ""
            else:
                continue
            self._remember(answer, replayed)
            return answer

    def prompt_yes_no(self, label: str, default: bool = False) -> bool:
        marker = "Y/n" if default else "y/N"
        value, replayed = self._ask(f"{label} [{marker}]: ")
        if replayed:
            return value == "yes"
        answer = default if not value else value.lower() in {"y", "yes"}
        self._remember("yes" if answer else "no", replayed)
        return answer


def suggested_local_repo_path(repo_name: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return f"~/code/{repo_name}"


def location_name_from_host(host: str) -> str:
    return host.rsplit("@", 1)[-1].split(".", 1)[0] or "remote"


def collect_wizard_config(path: Path, wizard: WizardPrompts) -> dict[str, Any] | None:
    if path.exists() and not wizard.prompt_yes_no("Replace the existing configuration?"):
        warning("Configuration unchanged")
        return None
    config = default_config()
    while True:
        wizard.explain(
            "Repository name is a short label for the project. It does not need "
            "to match GitHub.\n  Example: pimm\n"
        )
        repo_name = wizard.prompt("Repository name", Path.cwd().name)
        add_repo(config, repo_name)
        repo = require_repo(config, repo_name)

        wizard.explain(
            "\nLocal copy\n"
            "  hpsync always configures a copy on this computer. The repository path\n"
            "  is the directory where that Git checkout exists or should be created.\n"
            "  Run 'pwd' inside an existing checkout if you are unsure.\n"
        )
        local_path = wizard.prompt(
            "Local repository path", suggested_local_repo_path(repo_name)
        )
        add_location(
            config,
            repo_name,
            {
                "name": "local",
                "transport": "local",
                "path": local_path,
                "state": DEFAULT_STATE_PATH,
            },
        )
        wizard.explain(
            "  Location name: local (a label for this computer)\n"
            f"  Backup path: {DEFAULT_STATE_PATH} (safety copies before files are replaced)\n"
        )

        while True:
            if not wizard.prompt_yes_no(
                "Add another location?", default=len(repo["locations"]) < 2
            ):
                if len(repo["locations"]) < 2:
                    warning("At least two locations are needed to synchronize")
                    continue
                break
            wizard.explain(
                "\nAnother location\n"
                "  Transport says how this computer reaches it: 'ssh' for another\n"
                "  machine or site, and 'local' for another path on this computer.\n"
            )
            transport = wizard.prompt("Transport (ssh/local)", "ssh")
            if transport not in {"local", "ssh"}:
                warning("Transport must be local or ssh")
                continue
            host = ""
            if transport == "ssh":
                host = wizard.prompt("SSH host or alias (the name you pass to ssh)")
                default_name = location_name_from_host(host)
            else:
                default_name = f"local-{len(repo['locations']) + 1}"
            wizard.explain(
                "  Location name is only a memorable label, such as nersc or s3df."
            )
            location_name = wizard.prompt("Location name", default_name)
            wizard.explain(
                "  Repository path is where this same Git repository exists or should\n"
                "  be created at that location.\n"
            )
            location: dict[str, Any] = {
                "name": location_name,
                "transport": transport,
                "path": wizard.prompt("Repository path"),
            }
            if transport == "ssh":
                location["host"] = host
                wizard.explain(
                    "  Backup path stores safety archives on that machine. A scratch\n"
                    "  directory is a good choice on an HPC system.\n"
                )
                location["state"] = wizard.prompt("Backup path", DEFAULT_STATE_PATH)
                port = wizard.prompt("SSH port", required=False)
                if port:
                    location["port"] = int(port)
                auth = wizard.prompt("Authentication refresh command", required=False)
                if auth:
                    location["auth_command"] = shlex.split(auth)
            else:
                location["state"] = DEFAULT_STATE_PATH
            add_location(config, repo_name, location)
        wizard.explain(
            "\nExclusions\n"
            "  These optional names keep generated data, logs, checkpoints, or secrets\n"
            "  from being synchronized. Leave blank to use the safe defaults.\n"
        )
        extra_parts = wizard.prompt(
            "Additional excluded directory names (comma-separated)", required=False
        )
        if extra_parts:
            repo["exclude_parts"] = sorted(
                set(repo["exclude_parts"]).union(
                    item.strip() for item in extra_parts.split(",") if item.strip()
                )
            )
        extra_names = wizard.prompt(
            "Additional excluded file names (comma-separated)", required=False
        )
        if extra_names:
            repo["exclude_names"] = sorted(
                set(repo["exclude_names"]).union(
                    item.strip() for item in extra_names.split(",") if item.strip()
                )
            )
        if not wizard.prompt_yes_no("Add another repository?"):
            break
    return config


def config_wizard(path: Path) -> None:
    print(f"\n{title('hpsync configuration')}")
    print(
        f"  {path}\n\n"
        "Setup always includes a local copy. At least one location must already\n"
        "contain the repository; any other local or remote copies may be missing\n"
        "and can be created during setup.\n"
        "Type 'back' at any question to redo the previous answer.\n"
    )
    wizard = WizardPrompts()
    while True:
        try:
            config = collect_wizard_config(path, wizard)
            break
        except _RestartWizard:
            continue
    if config is None:
        return
    save_config(config, path)
    success(f"Saved {path}")
    print(
        "\nChecking which locations already contain each repository. Missing copies\n"
        "can be cloned from any configured copy that already exists.\n"
    )
    if bootstrap_config(config):
        success("Setup complete")
        print("  Next: run 'hpsync status' to review your worktrees.")
    else:
        warning("Configuration saved, but at least one checkout is still missing")


def cmd_config(args: argparse.Namespace) -> int:
    path = get_config_path(args.config_file)
    action = args.config_action or "wizard"
    if action in {"wizard", "init"}:
        config_wizard(path)
        return 0
    if action == "path":
        print(path)
        return 0
    if action == "show":
        print(json.dumps(load_config(path, allow_missing=args.allow_missing), indent=2))
        return 0
    if action == "validate":
        load_config(path)
        success(f"Valid configuration: {path}")
        return 0
    if action == "bootstrap":
        config = load_config(path)
        bootstrap_config(config, args.repositories, args.yes)
        return 0

    config = load_config(path, allow_missing=action == "add-repo")
    if action == "add-repo":
        add_repo(config, args.name)
    elif action == "remove-repo":
        remove_repo(config, args.name)
    elif action == "add-location":
        if not args.ssh and (args.port or args.python or args.auth_command):
            raise ConfigError("--port, --python, and --auth-command require --ssh")
        location: dict[str, Any] = {
            "name": args.name,
            "path": args.path,
            "transport": "ssh" if args.ssh else "local",
            "state": args.state,
        }
        if args.ssh:
            location["host"] = args.ssh
        if args.port:
            location["port"] = args.port
        if args.python:
            location["python"] = args.python
        if args.auth_command:
            location["auth_command"] = shlex.split(args.auth_command)
        add_location(config, args.repository, location, args.replace)
    elif action == "remove-location":
        remove_location(config, args.repository, args.name)
    save_config(config, path)
    success(f"Updated {path}")
    return 0


def add_worktree_arguments(parser: argparse.ArgumentParser, sync: bool = False) -> None:
    parser.add_argument("repositories", nargs="*", metavar="REPOSITORY")
    parser.add_argument("--location", dest="locations", action="append", default=[])
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default=os.environ.get("HPSYNC_COLOR", "auto"),
    )
    if sync:
        parser.add_argument("--yes", "-y", action="store_true", help="apply without confirmation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Safely synchronize uncommitted work across local and SSH Git worktrees.",
    )
    parser.add_argument("--config", dest="config_file", help="configuration file path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="inspect and compare working trees")
    add_worktree_arguments(status_parser)
    status_parser.set_defaults(func=cmd_worktrees)

    sync_parser = subparsers.add_parser("sync", help="back up and synchronize working trees")
    add_worktree_arguments(sync_parser, sync=True)
    sync_parser.set_defaults(func=cmd_worktrees)

    config_parser = subparsers.add_parser("config", help="create, inspect, and edit configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_action")
    for name in ("wizard", "init", "path", "validate"):
        config_subparsers.add_parser(name)
    bootstrap_parser = config_subparsers.add_parser(
        "bootstrap", help="create missing checkouts from an existing location"
    )
    bootstrap_parser.add_argument("repositories", nargs="*", metavar="REPOSITORY")
    bootstrap_parser.add_argument("--yes", "-y", action="store_true")
    show_parser = config_subparsers.add_parser("show")
    show_parser.add_argument("--allow-missing", action="store_true")
    add_repo_parser = config_subparsers.add_parser("add-repo")
    add_repo_parser.add_argument("name")
    remove_repo_parser = config_subparsers.add_parser("remove-repo")
    remove_repo_parser.add_argument("name")
    add_location_parser = config_subparsers.add_parser("add-location")
    add_location_parser.add_argument("repository")
    add_location_parser.add_argument("name")
    add_location_parser.add_argument("--path", required=True)
    transport = add_location_parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--local", action="store_true")
    transport.add_argument("--ssh", metavar="HOST")
    add_location_parser.add_argument("--port", type=int)
    add_location_parser.add_argument("--state", default=DEFAULT_STATE_PATH)
    add_location_parser.add_argument("--python")
    add_location_parser.add_argument("--auth-command")
    add_location_parser.add_argument("--replace", action="store_true")
    remove_location_parser = config_subparsers.add_parser("remove-location")
    remove_location_parser.add_argument("repository")
    remove_location_parser.add_argument("name")
    config_parser.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_color(getattr(args, "color", "auto"))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(f"\n{styled('!', '33')} Cancelled", file=sys.stderr)
        return 130
    except (ConfigError, OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"{styled('✗', '31')} {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
