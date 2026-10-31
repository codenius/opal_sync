# OPAL Sync

OPAL Sync downloads course materials from OPAL Bildungsportal Sachsen to a
folder on your computer. It keeps a local record of downloaded files so later
syncs can identify files OPAL reports as unchanged. When OPAL reports a new
last-modified date, the updated download is saved as a new version instead of
overwriting the existing local file.

## What you need

- Python 3.9 or newer.
- An OPAL account at a supported institution.
- An internet connection while logging in and syncing.

This project currently provides a command-line application. It has not yet
been published as a ready-to-install Windows installer or as a PyPI package.

## Install from a project folder

1. Install Python from [python.org](https://www.python.org/downloads/). On
   Windows, enable the option to add Python to `PATH` during installation.
2. Open PowerShell in the folder containing this project.
3. Install OPAL Sync:

   ```powershell
   py -m pip install .
   ```

The installed commands are `opal_sync` and `opal-sync`. If they are not
recognized after installation, close and reopen PowerShell and make sure
Python's Scripts directory is on `PATH`. You can also run the app as a Python
module:

```powershell
py -m opal_sync --help
```

## First-time setup

Run the interactive setup command:

```powershell
opal_sync setup
```

Setup asks you to sign in, choose where to save course folders and files, and
select courses to include or exclude. The default destination is the current
directory, except when setup is launched from your home directory, where it
defaults to a `opal_sync` subfolder. The configuration, database, log, and
OPAL session file are also stored in the chosen destination. Setup then offers
to run the initial sync. Your password is stored using the operating system's
credential store through Python `keyring`, not in the config file.

At interactive prompts, values shown in square brackets are defaults; press
Enter to accept them.

The generated `config.toml` can be edited later to change course filters or
other settings.

## Manual configuration

The default configuration file is named `config.toml` in the current working
directory. The easiest setup is to copy `config.example.toml` to
`config.toml` in the folder where you plan to run OPAL Sync:

```powershell
Copy-Item config.example.toml config.toml
```

Edit `config.toml` with a text editor if you want to change the download
folder or other settings. Relative paths such as `downloads`, `state.db`, and
`session.json` are resolved from the directory containing the config file.
The default `sync_directory = "."` means downloaded course folders go into
that same directory.

When running outside the config directory, pass `--config` to each command.
Log in and sync:

```powershell
opal_sync login --config "C:\Users\YourName\OPAL\config.toml"
opal_sync sync --config "C:\Users\YourName\OPAL\config.toml"
```

Enter your institution, username, and password when prompted. After successful
login, the password is stored using the operating system's credential store
through Python `keyring`; the config file does not need to contain your
password.

## Common commands

```text
opal_sync setup                   Guided first-time setup
opal_sync login                   Sign in to OPAL
opal_sync sync                    Download or update course files
opal_sync sync --course "Math"    Sync courses whose name matches "Math"
opal_sync sync --list-skipped-files
                                  Show files skipped by extension or size filters
opal_sync sync --force            Request downloads even when files look unchanged
opal_sync courses                 Show cached courses
opal_sync courses --refresh       Refresh the course list from OPAL
opal_sync status                  Show sync and storage status
opal_sync log                     Show recent sync activity
opal_sync logout                  Clear saved credentials and session cookies
opal_sync config --path           Show the active config file location
```

Run commands from the sync directory to use its `config.toml` automatically.
From another directory, pass the config file explicitly:

```powershell
opal_sync sync --config "C:\Users\YourName\OPAL\config.toml"
```

## Configuration

The main settings are:

| Setting | Default | Description |
|---|---|---|
| `general.sync_directory` | `"."` | Where course folders are saved |
| `general.log_level` | `"INFO"` | Logging detail: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `general.log_file` | `"opal-sync.log"` | Log file location |
| `general.db_file` | `"state.db"` | Local sync-state database |
| `auth.session.cookie_file` | `"session.json"` | Saved OPAL session cookies |
| `sync.include_extensions` | `[]` | Optional extension allowlist; empty includes all extensions |
| `sync.exclude_extensions` | `[]` | Extensions to skip; an extension also explicitly included is allowed |
| `sync.max_file_size_mb` | `500` | Maximum remote file size to download |
| `sync.parallel_downloads` | `3` | Maximum concurrent downloads |
| `sync.request_delay_seconds` | `1.0` | Delay between requests |
| `courses.include` | `[]` | Optional case-insensitive course-name filters |
| `courses.exclude` | `[]` | Course names to exclude |

Course include/exclude matching is case-insensitive and based on whether the
configured text appears in a course name. Excluded course matches take
precedence. Per-course folder overrides can be set under `[courses.paths]`;
relative paths are placed under `sync_directory`. Downloaded folders mirror
OPAL's nested course structure. A top-level structure node named `Unterlagen`
is treated as the course root and omitted from the local path.

Settings not specified in `config.toml` use the defaults shown above. The
`config.example.toml` file contains a complete commented example.

## Troubleshooting

- **The command cannot find my config:** run it from the sync directory, or
  pass the config file explicitly with `--config`.
- **Login fails:** confirm you selected the right institution and can sign in
  to OPAL in a browser. Run `opal-sync login` again if your credentials need
  to be updated.
- **I cannot find downloaded files:** check `general.sync_directory` in the
  active config. Relative paths are based on the config file's directory.
- **A sync failed:** inspect the configured log file and the output from
  `opal-sync status`. Do not share passwords, session cookies, or private
  course files when reporting an issue.
- **A local file has my annotations:** the sync decision does not use local
  file size or ETag. OPAL's recognized last-modified date is used to detect
  remote updates.

## Development and tests

To install development dependencies and run the test suite:

```powershell
py -m pip install ".[dev]"
py -m pytest
```
