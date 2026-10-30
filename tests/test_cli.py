from io import StringIO
from rich.console import Console
from typer.testing import CliRunner
from opal_sync.cli import app
from opal_sync.config import AppConfig
from opal_sync.sync.differ import SyncAction
from opal_sync.sync.engine import SyncItemReport, SyncSummary

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Automated synchronization client for OPAL" in result.output
    assert "login" in result.output
    assert "sync" in result.output
    assert "courses" in result.output
    assert "status" in result.output


def test_cli_config_show(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
    [general]
    sync_directory = "~/Documents/OPAL"
    log_level = "DEBUG"
    """)

    result = runner.invoke(app, ["config", "--config", str(cfg_file)])
    assert result.exit_code == 0
    assert "sync_directory" in result.output


def test_cli_status(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
    [general]
    sync_directory = "~/Documents/OPAL"

    [auth]
    username = "s12345"
    university = "htwk-leipzig"
    """)

    result = runner.invoke(app, ["status", "--config", str(cfg_file)])
    assert result.exit_code == 0
    assert "htwk-leipzig" in result.output
    assert "s12345" in result.output


def test_sync_lists_only_excluded_and_oversized_files_before_summary(monkeypatch):
    class FakeEngine:
        config = AppConfig()

        async def run_sync(self, **_kwargs):
            return SyncSummary(
                items=[
                    SyncItemReport(
                        course_name="Physics",
                        node_title="Lectures",
                        filename="video.mp4",
                        action=SyncAction.SKIP_EXCLUDED,
                        details="File extension is excluded by sync configuration",
                    ),
                    SyncItemReport(
                        course_name="Physics",
                        node_title="Lectures",
                        filename="large.pdf",
                        action=SyncAction.SKIP_TOO_LARGE,
                        details="Size 600.0MB exceeds limit of 500.0MB",
                    ),
                    SyncItemReport(
                        course_name="Physics",
                        node_title="Lectures",
                        filename="unchanged.pdf",
                        action=SyncAction.SKIP_UNCHANGED,
                        details="OPAL last-modified date is unchanged",
                    ),
                ],
            )

    output = StringIO()
    monkeypatch.setattr(
        "opal_sync.cli.console",
        Console(file=output, force_terminal=True, color_system="standard"),
    )
    monkeypatch.setattr("opal_sync.cli.get_configured_engine", lambda _path: FakeEngine())
    result = runner.invoke(app, ["sync", "--list-skipped-files"])
    rendered = output.getvalue()

    assert result.exit_code == 0
    assert "Physics/Lectures/\x1b[36mvideo.mp4\x1b[0m (extension:" in rendered
    assert "Physics/Lectures/\x1b[36mlarge.pdf\x1b[0m (size:" in rendered
    assert "unchanged.pdf" not in rendered
    assert rendered.index("Skipped files:") < rendered.index(
        "Synchronization Summary"
    )


def test_sync_displays_connection_error_in_panel_and_returns_failure(monkeypatch):
    class FakeEngine:
        config = AppConfig()

        async def run_sync(self, **_kwargs):
            return SyncSummary(
                status="error",
                error_message=(
                    "Unable to connect to OPAL. Check your internet connection "
                    "and try again."
                ),
            )

    monkeypatch.setattr("opal_sync.cli.get_configured_engine", lambda _path: FakeEngine())
    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 1
    assert "Sync failed" in result.output
    assert "Check your internet connection" in result.output
