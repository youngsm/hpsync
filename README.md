# hpsync = hpc + sync

`hpsync` safely synchronizes
uncommitted Git work across any number of worktrees on your local computer
and SSH-accessible machines.

It is designed for work that is not ready to commit but needs to follow you
between a laptop, workstation, login node, or compute site. Each
worktree is assumed to be at the same commit.

## Safety model

Before changing anything, `hpsync`:

1. verifies that every worktree is based on the same Git commit;
2. detects active merges, rebases, cherry-picks, and similar operations;
3. hashes every changed file at every location;
4. blocks paths that were changed differently in multiple places;
5. shows the complete transfer plan and asks for confirmation;
6. creates a compressed backup at every location that will be modified;
7. verifies that all worktrees converge after the transfer.

Files are copied directly between worktrees. `hpsync` does not commit, pull,
push, reset, or modify Git history.

## Install

Python 3.10 or newer is required on the computer running `hpsync`. SSH
locations need `python3`, `git`, and `tar`.

```sh
python -m pip install --user .
```

The command is installed as `hpsync`.

## Configure

Run the interactive setup:

```sh
hpsync config
```

The wizard separates each question with a terminal-width dim gray rule and
explains each value as it asks for it. In short:

- **Repository name** is a label for the project, such as `pimm`.
- **Location name** is a label for a computer or site, such as `local`, `nersc`,
  or `s3df`.
- **Transport** is `local` for a path on this computer or `ssh` for another
  machine.
- **Repository path** is where the Git checkout exists or should be created on
  that machine.
- **Backup path** is where safety archives are stored before files are replaced.

Setup always adds a location named `local`. At least one configured location
must already contain the repository, but the local copy or any remote copy may
be missing. The wizard detects missing checkouts and offers to clone them from
an existing location using a temporary Git bundle. An existing checkout does
not need a Git `origin` for this initial bootstrap.

The default configuration is `~/.config/hpsync/config.json`. Override it with
`HPSYNC_CONFIG` or the global `--config PATH` option.

You can also build the configuration without the wizard:

```sh
hpsync config add-repo my-project

hpsync config add-location my-project laptop \
  --local \
  --path ~/code/my-project

hpsync config add-location my-project workstation \
  --ssh workstation \
  --path ~/code/my-project

hpsync config add-location my-project cluster \
  --ssh user@login.example.org \
  --path /work/user/my-project \
  --state /scratch/user/hpsync
```

For a site that needs a credential refresh command, configure it explicitly:

```sh
hpsync config add-location my-project nersc \
  --ssh nersc \
  --path /global/u1/u/user/my-project \
  --state /pscratch/sd/u/user/hpsync \
  --auth-command sshproxy
```

Useful configuration commands:

```sh
hpsync config show
hpsync config validate
hpsync config path
hpsync config bootstrap
hpsync config remove-location my-project cluster
hpsync config remove-repo my-project
```

The generated JSON is intentionally straightforward and can be edited by hand:

```json
{
  "version": 1,
  "repositories": [
    {
      "name": "my-project",
      "locations": [
        {
          "name": "laptop",
          "transport": "local",
          "path": "~/code/my-project",
          "state": "~/.local/state/hpsync"
        },
        {
          "name": "cluster",
          "transport": "ssh",
          "host": "user@login.example.org",
          "path": "/work/user/my-project",
          "state": "/scratch/user/hpsync"
        }
      ],
      "exclude_parts": [".git", ".venv", "__pycache__"],
      "exclude_names": [".env"]
    }
  ]
}
```

`exclude_parts` matches a directory or path component anywhere in the
repository. `exclude_names` matches an exact file name. Add generated data,
logs, checkpoints, or secrets that should never move between machines.

## Use

Inspect all configured repositories without changing anything:

```sh
hpsync status
```

Review, back up, synchronize, and verify:

```sh
hpsync sync
```

Limit an operation to named repositories or locations:

```sh
hpsync status my-project --verbose
hpsync sync my-project --location laptop --location workstation
```

Use `--yes` for an already-reviewed, non-interactive synchronization:

```sh
hpsync sync my-project --yes
```

## How conflicts are decided

For each dirty path, all configured locations are compared. If one file state
is the only changed version, it is copied to every location that differs. If
several locations contain the same changed version, they agree and that version
still propagates. If changed locations disagree, the path is reported as a
conflict and the synchronization is blocked.

If locations have different `HEAD` commits, you must align them with your normal Git workflow before running
`hpsync` again.

## Development

```sh
python -m unittest discover -v
```

## License

MIT
