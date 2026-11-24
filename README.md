# OPAL Sync
[![Build Windows binary](https://github.com/codenius/opal_sync/actions/workflows/build-windows.yml/badge.svg)](https://github.com/codenius/opal_sync/actions/workflows/build-windows.yml)
[![Latest release](https://img.shields.io/github/v/release/codenius/opal_sync)](https://github.com/codenius/opal_sync/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)  
Keep your OPAL course materials synced to a folder.

## What is this?

OPAL Sync **downloads course materials** from the **OPAL Bildungsportal Sachsen** to your computer, cleanly **organized into folders**. It tracks downloaded files between runs to **efficiently** find and **download new files, for updated files the new version is added**.  

Deleted files are *always kept* so your annotations or changes stay put. All file changes discoverd form OPAL during a sync run are displayed, so you know what is new.  
OPAL Sync is a *cross-plattform* command-line application based on Python.

## Quick start for Windows (recommended)

1. [⭳ Download](https://github.com/codenius/opal_sync/releases/latest/download/opal_sync.exe) the latest [`opal_sync.exe`](https://github.com/codenius/opal_sync/releases/latest/download/opal_sync.exe).
2. Create a new folder for your synced course files.
3. Place `opal_sync.exe` in that folder.
4. Double-click `opal_sync.exe`.
5. Follow the command-line prompts to sign in, choose the sync directory, and select courses. You can accept the suggested defaults by pressing Enter. When prompted, choose whether to run the initial sync.
6. After setup, sync anytime by double-clicking `opal_sync.exe`

> [!NOTE] 
> If security prompted click 'More information' and 'Run anyway' to continue. OPAL sync is safe, but official signing is expensive.
## Install and run (all OSes)
> [!TIP] 
> A standalone Windows executable is available from [GitHub Releases](https://github.com/codenius/opal_sync/releases/latest); no Python installation or virtual environment is needed.

On Linux, macOS or Windows, install Python 3.9 or newer, then choose either option:

**pip** — install directly from GitHub with Python 3.9 or newer:

```text
python -m pip install git+https://github.com/codenius/opal_sync.git
opal_sync setup
```
Use `python3` instead of `python` if that is how Python 3 is named on your system.


**Alternative: uv** — uv installs OPAL Sync in an isolated environment. [Install uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```text
uv tool install git+https://github.com/codenius/opal_sync.git
opal_sync setup
```

During setup, enter your institution and account details, choose where course files should be saved, and select courses. The app offers an initial sync at the end. Your password is stored in the operating system's credential store when supported by the platform's keyring backend; it is not saved in the config file.

## Common commands

Run commands from the directory containing the configuration created during setup. Add `--config <path>` if you run them from elsewhere.

```text
opal_sync sync                    Download or update course files
opal_sync sync --course "Math"    Sync courses whose name matches "Math"
opal_sync sync --list-skipped-files
                                  Show files skipped by extension or size filters
opal_sync sync --rescan           Recrawl folders with unchanged dates, skipping unchanged files
opal_sync courses                 Show cached courses
opal_sync courses --refresh       Refresh the course list from OPAL
opal_sync status                  Show sync and storage status
opal_sync log                     Show recent sync activity
opal_sync logout                  Clear saved credentials and session cookies
opal_sync config --path           Show the active config file location
```

## Configuration reference

Setup creates `config.toml` in the chosen sync directory. Most users do not need to edit it. The settings below are the application defaults; setup may save your selected values instead.

| Setting | Default | Description |
|---|---|---|
| `general.sync_directory` | `"."` | Where course folders are saved |
| `general.log_level` | `"INFO"` | Logging detail: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `general.log_file` | `"opal-sync.log"` | Log file location; oldest half pruned above 2,500 lines |
| `general.db_file` | `"state.db"` | Local sync-state database |
| `auth.university` | `"htwk-leipzig"` | Institution identifier used for sign-in |
| `auth.session.cookie_file` | `"session.json"` | Saved OPAL session cookies |
| `auth.session.session_ttl_hours` | `1` | Session lifetime before re-authentication |
| `auth.session.max_retry_auth` | `3` | Authentication retry limit |
| `sync.include_extensions` | `[]` | Optional extension allowlist; empty includes all extensions |
| `sync.exclude_extensions` | `[]` | Extensions to skip; explicitly included extensions are allowed |
| `sync.max_file_size_mb` | `50` | Maximum remote file size to download; `0` disables the limit |
| `sync.parallel_downloads` | `3` | Maximum concurrent downloads |
| `sync.request_delay_seconds` | `0.5` | Delay between requests |
| `sync.delete_removed` | `false` | Whether to delete local files removed from OPAL |
| `courses.include` | `[]` | Optional case-insensitive course-name filters; empty includes all enrolled courses |
| `courses.exclude` | `[]` | Course names to exclude |
| `courses.paths` | `{}` | Optional per-course folder overrides |

The configured log is a single file. When file logging is initialized, an existing log with more than 2,500 physical lines is pruned by deleting its oldest half. The log can exceed 2,500 lines during a command; it is pruned the next time a command initializes file logging. Only one process should write to a given log file at a time.

Course filters match text within course names without regard to case. Excluded matches take precedence. Relative paths for per-course overrides are placed under the sync directory. Downloaded folders mirror OPAL's nested course structure; a top-level `Unterlagen` node is omitted from local paths.

## Troubleshooting

- **The command cannot find the configuration:** run it from the sync directory or provide the path with `--config`.
- **Login fails:** check that you selected the right institution and can sign in to OPAL in a browser, then run `opal_sync login` again.
- **Files are not where expected:** check the sync directory shown by `opal_sync status`.
- **A sync failed:** inspect the log file and `opal_sync status`. Do not share passwords, session cookies, or private course files when reporting an issue.

## Development

Development requires [uv](https://docs.astral.sh/uv/).

Install the locked development environment and run the test suite:

```text
uv sync --locked --extra dev
uv run --locked pytest
```

Build the standalone Windows executable:

```text
build.bat
```

The script uses `uv` to create or update `.venv` and install the locked build dependencies.
The output is `dist/opal_sync.exe`. It is a console application; the Windows `keyring` backend is included in the PyInstaller spec.
