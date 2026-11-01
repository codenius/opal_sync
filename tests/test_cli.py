from io import StringIO
import pytest
from rich.console import Console
from typer.testing import CliRunner
from opal_sync.cli import app
from opal_sync.config import AppConfig, load_config
from opal_sync.models.course import Course
from opal_sync.sync.differ import SyncAction
from opal_sync.sync.engine import SyncItemReport, SyncSummary

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Automated synchronization client for OPAL" in result.output
    assert "opal_sync" in result.output
    assert "from any directory" in result.output
    assert "login" in result.output
    assert "setup" in result.output
    assert "sync" in result.output
    assert "courses" in result.output
    assert "status" in result.output


def test_cli_without_command_without_config_offers_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    setup_called = []
    monkeypatch.setattr("opal_sync.cli.setup", lambda: setup_called.append(True))

    result = runner.invoke(app, [], input="y\n")

    assert result.exit_code == 0
    # The current welcome banner is rendered as block-character artwork.
    assert "No configuration found." in result.output
    assert "Would you like to start setup now?" in result.output
    assert setup_called == [True]


def test_cli_without_command_without_config_can_decline_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("opal_sync.cli.setup", lambda: pytest.fail("setup should not run"))

    result = runner.invoke(app, [], input="n\n")

    assert result.exit_code == 0
    assert "No configuration found." in result.output


def test_cli_without_command_with_config_starts_sync(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("")
    sync_calls = []
    monkeypatch.setattr(
        "opal_sync.cli.sync",
        lambda **kwargs: sync_calls.append(kwargs),
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert sync_calls == [{
        "dry_run": False,
        "course": None,
        "force": False,
        "rescan": False,
        "list_skipped_files": False,
        "config_path": (tmp_path / "config.toml").resolve(),
    }]


def test_root_level_invalid_option_shows_setup_card_and_help(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["--config", ".\\opal sync"])

    assert result.exit_code == 2
    assert "No such option '--config'." in result.output
    assert "No config detected." in result.output
    assert "opal_sync setup" in result.output
    assert "Usage:" in result.output
    assert "Commands" in result.output


def test_root_level_invalid_option_with_config_suggests_sync(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("")

    result = runner.invoke(app, ["--config", ".\\opal sync"])

    assert result.exit_code == 2
    assert "No such option '--config'." in result.output
    assert "No config detected." not in result.output
    assert "opal_sync sync" in result.output
    assert "Usage:" in result.output


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


def test_config_option_is_supported(tmp_path):
    config_path = tmp_path / "config.toml"
    result = runner.invoke(app, ["config", "--path", "--config", str(config_path)])

    assert result.exit_code == 0
    assert str(config_path) in result.output.replace("\n", "").replace("\r", "")


def test_setup_saves_selected_filters_and_teaches_sync_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target_directory = tmp_path / "Nextcloud" / "Studium Elektrotechnik" / "1. Semester"
    target_directory.mkdir(parents=True)

    class FakeDatabase:
        def upsert_course(self, _course):
            pass

    class FakeEngine:
        def __init__(self):
            self.db = FakeDatabase()

        def is_course_allowed(self, course_name):
            return course_name == "Mathematics"

        async def ensure_authenticated(self, _client):
            pass

    engine = FakeEngine()

    def fake_authenticate(config, show_header=True):
        config.auth.username = "test-user"
        return [
            {
                "name": "JSESSIONID",
                "value": "test-cookie",
                "domain": "example.invalid",
                "path": "/",
                "secure": True,
            }
        ]

    monkeypatch.setattr("opal_sync.cli._authenticate_user", fake_authenticate)
    monkeypatch.setattr(
        "opal_sync.cli.get_configured_engine",
        lambda _config_path: engine,
    )

    async def fetch_courses(_client):
        return [
            Course(opal_id="1", name="Mathematics", url="https://example.invalid/1"),
            Course(opal_id="2", name="History", url="https://example.invalid/2"),
        ]

    monkeypatch.setattr("opal_sync.cli.fetch_my_courses", fetch_courses)
    monkeypatch.setattr(
        "opal_sync.cli._select_course_filters",
        lambda _courses, _include, _exclude: (["Mathematics"], ["History"]),
    )
    result = runner.invoke(
        app,
        ["setup"],
        input=f'"{target_directory}"\nn\n',
    )

    config_path = target_directory / "config.toml"
    config = load_config(config_path)
    assert result.exit_code == 0, result.output
    assert config.general.sync_directory == target_directory
    assert config.general.log_file == target_directory / "opal-sync.log"
    assert config.general.db_file == target_directory / "state.db"
    assert config.auth.session.cookie_file == target_directory / "session.json"
    assert config.auth.username == "test-user", result.output
    assert config.courses.include == ["Mathematics"]
    assert config.courses.exclude == ["History"]
    assert "opal_sync sync --config" in result.output
    assert "Run a sync whenever you need" in result.output
    assert "Values in brackets are" in result.output
    assert "--dry-run" not in result.output
    assert "Where should OPAL Sync save course folders and files?" in result.output
    assert "Your sync commands" in result.output
    assert (target_directory / "session.json").is_file()


def test_setup_defaults_to_opal_sync_subfolder_from_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("opal_sync.cli.Path.home", lambda: tmp_path)

    class FakeDatabase:
        def upsert_course(self, _course):
            pass

    class FakeEngine:
        db = FakeDatabase()

        async def ensure_authenticated(self, _client):
            pass

    monkeypatch.setattr(
        "opal_sync.cli._authenticate_user",
        lambda _config, show_header=True: [],
    )
    monkeypatch.setattr(
        "opal_sync.cli.get_configured_engine",
        lambda _config_path: FakeEngine(),
    )

    async def fetch_courses(_client):
        return []

    monkeypatch.setattr("opal_sync.cli.fetch_my_courses", fetch_courses)
    result = runner.invoke(app, ["setup"], input="\ny\nn\n")

    assert result.exit_code == 0, result.output
    assert (tmp_path / "opal_sync" / "config.toml").is_file()
    assert "opal_sync" in result.output


def test_path_input_accepts_windows_and_posix_separators(tmp_path, monkeypatch):
    from opal_sync.cli import _normalize_path_input

    monkeypatch.chdir(tmp_path)
    expected = (tmp_path / "courses" / "semester").resolve()

    assert _normalize_path_input("courses/semester") == expected
    assert _normalize_path_input("courses\\semester") == expected
    assert _normalize_path_input(
        str(tmp_path / "courses" / "semester")
    ) == expected
    assert _normalize_path_input(
        str(tmp_path / "courses" / "semester").replace("\\", "/")
    ) == expected


def test_sync_directory_prompt_requires_confirmation_to_create(monkeypatch, tmp_path):
    from opal_sync.cli import _prompt_sync_directory

    first_path = tmp_path / "declined"
    second_path = tmp_path / "created"
    paths = iter([str(first_path), str(second_path)])
    confirmations = iter([False, True])
    questions = []

    monkeypatch.setattr("opal_sync.cli.typer.prompt", lambda *_args, **_kwargs: next(paths))

    def confirm(message, **kwargs):
        questions.append(message)
        assert kwargs["default"] is True
        return next(confirmations)

    monkeypatch.setattr("opal_sync.cli.typer.confirm", confirm)

    assert _prompt_sync_directory(tmp_path / "default") == second_path.resolve()
    assert not first_path.exists()
    assert second_path.is_dir()
    assert len(questions) == 2


def test_course_filters_from_positions_are_mutually_exclusive():
    from opal_sync.cli import _course_filters_from_positions

    courses = [
        Course(opal_id="1", name="Mathematics", url="https://example.invalid/1"),
        Course(opal_id="2", name="History", url="https://example.invalid/2"),
    ]

    assert _course_filters_from_positions(courses, [0, 0]) == ([], [])
    assert _course_filters_from_positions(courses, [1, -1]) == (
        ["Mathematics"],
        ["History"],
    )


def test_unset_courses_are_excluded_only_when_include_filter_is_used():
    from opal_sync.cli import _course_sync_states

    assert _course_sync_states([0, 0, -1]) == [True, True, False]
    assert _course_sync_states([1, 0, -1]) == [True, False, False]


def test_course_picker_arrow_keys_move_courses_between_states(monkeypatch):
    from prompt_toolkit.keys import Keys
    from opal_sync.cli import _select_course_filters

    class FakeApplication:
        def __init__(self, *, key_bindings, **_kwargs):
            self.bindings = {binding.keys: binding.handler for binding in key_bindings.bindings}

        def invalidate(self):
            pass

        def exit(self):
            pass

        def press(self, key):
            self.bindings[(key,)](type("Event", (), {"app": self})())

        def run(self):
            self.press(Keys.Right)
            self.press(Keys.Left)
            self.press(Keys.Down)
            self.press(Keys.Left)
            self.press(Keys.Left)
            self.press(Keys.Right)
            self.press(Keys.Left)
            self.press(Keys.Enter)

    monkeypatch.setattr("opal_sync.cli.Application", FakeApplication)
    courses = [
        Course(opal_id="1", name="Mathematics", url="https://example.invalid/1"),
        Course(opal_id="2", name="History", url="https://example.invalid/2"),
    ]

    assert _select_course_filters(courses, [], []) == ([], ["History"])


def test_course_picker_ctrl_c_aborts(monkeypatch):
    from prompt_toolkit.keys import Keys
    from opal_sync.cli import _select_course_filters

    class FakeApplication:
        def __init__(self, *, key_bindings, **_kwargs):
            self.bindings = {binding.keys: binding.handler for binding in key_bindings.bindings}

        def exit(self, **kwargs):
            if kwargs.get("exception"):
                raise kwargs["exception"]

        def run(self):
            self.bindings[(Keys.ControlC,)](
                type("Event", (), {"app": self})()
            )

    monkeypatch.setattr("opal_sync.cli.Application", FakeApplication)
    courses = [
        Course(opal_id="1", name="Mathematics", url="https://example.invalid/1"),
    ]

    with pytest.raises(KeyboardInterrupt):
        _select_course_filters(courses, [], [])


def test_login_retries_when_idp_returns_no_saml_assertion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    passwords = []
    password_prompts = iter(["wrong-password", "correct-password"])

    def prompt(message, **_kwargs):
        if message == "Select university":
            return "htwk-leipzig"
        if message == "University username":
            return "test-user"
        if message == "University Password":
            return next(password_prompts)
        raise AssertionError(f"Unexpected prompt: {message}")

    async def login_shibboleth(client, username, password, wayf_value):
        passwords.append(password)
        if password == "wrong-password":
            from opal_sync.auth.shibboleth import ShibbolethAuthError

            raise ShibbolethAuthError(
                "IdP did not return a SAMLResponse assertion. "
                "Check your credentials or HTWK account state."
            )
        client.cookies.set(
            "OPALSESSION",
            "test-session",
            domain="bildungsportal.sachsen.de",
            path="/",
        )
        return True

    monkeypatch.setattr("opal_sync.cli.typer.prompt", prompt)
    monkeypatch.setattr("opal_sync.cli.login_shibboleth", login_shibboleth)
    monkeypatch.setattr("opal_sync.cli.store_credentials", lambda *_args: None)

    result = runner.invoke(app, ["login"])

    assert result.exit_code == 0, result.output
    assert passwords == ["wrong-password", "correct-password"]
    assert "Password characters are hidden" in result.output
    assert "Configuration saved to:" in result.output


def test_authentication_allows_retry_after_wrong_password(monkeypatch):
    from opal_sync.cli import _authenticate_user

    passwords = []
    password_prompts = iter(["wrong-password", "correct-password"])

    def prompt(message, **_kwargs):
        if message == "Select university":
            return "htwk-leipzig"
        if message == "University username":
            return "test-user"
        if message == "University Password":
            return next(password_prompts)
        raise AssertionError(f"Unexpected prompt: {message}")

    async def login(client, username, password, wayf_value):
        passwords.append(password)
        if password == "wrong-password":
            from opal_sync.auth.shibboleth import ShibbolethAuthError

            raise ShibbolethAuthError("Invalid username or password for HTWK login.")
        client.cookies.set(
            "OPALSESSION",
            "test-session",
            domain="bildungsportal.sachsen.de",
            path="/",
        )
        return True

    monkeypatch.setattr("opal_sync.cli.typer.prompt", prompt)
    monkeypatch.setattr("opal_sync.cli.login_shibboleth", login)
    monkeypatch.setattr("opal_sync.cli.store_credentials", lambda *_args: None)

    cookies = _authenticate_user(AppConfig())

    assert passwords == ["wrong-password", "correct-password"]
    assert any(cookie["name"] == "OPALSESSION" for cookie in cookies)


def test_authentication_retries_username_and_password_if_username_not_found(monkeypatch):
    from opal_sync.cli import _authenticate_user
    from opal_sync.auth.shibboleth import ShibbolethAuthError

    prompts = iter([
        ("Select university", "htwk-leipzig"),
        ("University username", ""),
        ("University username", "unknown-user"),
        ("University Password", "first-password"),
        ("University username", "correct-user"),
        ("University Password", "correct-password"),
    ])
    attempts = []

    def prompt(message, **kwargs):
        expected, value = next(prompts)
        assert message == expected
        if message == "University username":
            assert "default" not in kwargs
        if message == "University Password":
            assert kwargs.get("hide_input") is True
        return value

    async def login(client, username, password, wayf_value):
        attempts.append((username, password))
        if username == "unknown-user":
            raise ShibbolethAuthError(
                "Invalid username or password for HTWK login: "
                "Der eingegebene Benutzername wurde nicht gefunden."
            )
        client.cookies.set(
            "OPALSESSION",
            "test-session",
            domain="bildungsportal.sachsen.de",
            path="/",
        )
        return True

    monkeypatch.setattr("opal_sync.cli.typer.prompt", prompt)
    monkeypatch.setattr("opal_sync.cli.login_shibboleth", login)
    monkeypatch.setattr("opal_sync.cli.store_credentials", lambda *_args: None)

    _authenticate_user(AppConfig())

    assert attempts == [
        ("unknown-user", "first-password"),
        ("correct-user", "correct-password"),
    ]


def test_setup_can_run_initial_sync(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sync_calls = []

    class FakeDatabase:
        def upsert_course(self, _course):
            pass

    class FakeEngine:
        db = FakeDatabase()

        async def ensure_authenticated(self, _client):
            pass

        async def run_sync(self):
            sync_calls.append(True)
            return SyncSummary(courses_processed=2, files_new=3)

    monkeypatch.setattr(
        "opal_sync.cli._authenticate_user",
        lambda _config, show_header=True: [],
    )
    monkeypatch.setattr(
        "opal_sync.cli.get_configured_engine",
        lambda _config_path: FakeEngine(),
    )

    async def fetch_courses(_client):
        return []

    monkeypatch.setattr("opal_sync.cli.fetch_my_courses", fetch_courses)
    result = runner.invoke(app, ["setup"], input="\n\n y\n")

    assert result.exit_code == 0, result.output
    assert sync_calls == [True]
    assert "3 new files" in result.output


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


def test_sync_without_config_suggests_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 1
    assert "No config file found" in result.output
    assert "No config detected." in result.output
    assert "Getting started" in result.output
    assert "opal_sync setup" in result.output
    assert "Usage:" not in result.output


def test_sync_lists_only_excluded_and_oversized_files_before_summary(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
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
    result = runner.invoke(
        app, ["sync", "--config", str(config_path), "--list-skipped-files"]
    )
    rendered = output.getvalue()

    assert result.exit_code == 0
    assert "Physics/Lectures/video.mp4 (extension:" in rendered
    assert "Physics/Lectures/large.pdf (size:" in rendered
    assert "unchanged.pdf" not in rendered
    assert rendered.index("Skipped files:") < rendered.index(
        "Synchronization Summary"
    )


def test_sync_displays_connection_error_in_panel_and_returns_failure(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.toml"
    config_path.write_text("")
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
    result = runner.invoke(app, ["sync", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "Sync failed" in result.output
    assert "Check your internet connection" in result.output
