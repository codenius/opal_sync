# OPAL Sync technical overview

This document is a codebase orientation for contributors and maintainers. For
installation, configuration, and CLI usage, see the repository
[README](../README.md).

## Runtime flow

The `opal-sync` console entry point is registered in `pyproject.toml` and
loads the Typer application in `src/opal_sync/cli.py`. CLI commands load the
TOML configuration through `config.py`; sync-related commands create a
`SyncEngine` with the configured paths, logger, SQLite database, and session
manager.

A sync follows this high-level path:

1. `SyncEngine.ensure_authenticated` checks the current HTTP client's session,
   tries saved cookies, then authenticates with the configured username and a
   password from the OS keyring (or an explicit development configuration).
   `auth/shibboleth.py` performs the OPAL WAYF selection, IdP redirects,
   credential submission, and SAML response hand-off. A successful session
   is saved by `auth/session.py`.
2. `scraper/courses.py` fetches the enrolled-course page and parses course
   IDs, names, URLs, and semester labels.
3. `scraper/course_tree.py` discovers file-bearing course nodes. It prefers
   the embedded jsTree data and falls back to parsing course-node links.
4. `scraper/folder.py` and `scraper/file_links.py` traverse folders, including
   Wicket AJAX-backed folders, and extract file URLs, paths, and remote
   metadata. `scraper/wicket.py` parses Wicket AJAX responses.
5. `sync/differ.py` compares discovered files and metadata with local files
   and database records. `sync/downloader.py` fetches permitted files with
   concurrency, request-delay, size, and extension controls. Conflict helpers
   in `sync/conflict.py` avoid overwriting local files when remote paths
   change or updated versions arrive.
6. `storage/db.py` records courses, nodes, folder state, file state, and sync
   runs in SQLite. Dry runs report planned work without recording a sync run
   or downloading files.

The main orchestration is in `sync/engine.py`. Prefer keeping HTTP operations
async and passing the shared `httpx.AsyncClient` through the authentication,
scraping, and download layers.

## Package map

- `cli.py`: Typer commands and user-facing output.
- `config.py`: TOML parsing, defaults, validation, and resolved paths.
- `auth/`: Shibboleth authentication, keyring credentials, and cookie
  persistence.
- `models/`: shared course, node, and file-entry data classes.
- `scraper/`: OPAL page parsing, folder traversal, file-link extraction, and
  Wicket response handling.
- `sync/`: orchestration, diff decisions, download behavior, and path
  conflicts.
- `storage/`: SQLite schema and persistence operations.
- `utils/`: logging, extension filters, and safe local path components.
- `tests/`: unit tests, mocked HTTP interactions, and HTML fixtures.

Configuration defaults and their user-facing descriptions are maintained in
`config.example.toml` and `README.md`. Configuration paths are resolved by
`config.py`; relative configured paths are based on the config file's
directory.

## Tests and fixtures

Install development dependencies and run the test suite from the repository
root:

```powershell
py -m pip install ".[dev]"
py -m pytest
```

Tests use pytest, pytest-asyncio, and respx to exercise behavior without
requiring a live OPAL account. `tests/conftest.py` loads shared fixtures;
scraper tests also use representative captured OPAL pages under
`tests/fixtures/real/`.

When adding or refreshing fixtures, remove personal identifiers and live
authentication material (including session IDs and CSRF values) before
committing. Replace them with stable synthetic values and update tests to
assert behavior rather than production tokens. Keep fixtures representative
of the markup and metadata required by the parser.

For a change, add or update the closest unit test first. Authentication and
network behavior should use mocked responses; do not make automated tests
depend on OPAL availability or real credentials.

## Local development and data boundaries

The top-level `scripts/` directory contains local development and
investigation scripts. It is intentionally ignored by Git and is not required
by the installed package, test suite, or normal CLI workflow. Keep it on the
developer's machine; do not distribute its contents.

Credentials belong in the OS keyring, not in source files or committed
configuration. Session cookies, local databases, logs, downloads, virtual
environments, and build artifacts are local data and should remain
uncommitted. OPAL pages and its Shibboleth/Wicket behavior may change, so
scraper assumptions should be covered by fixtures and focused tests.
