from pathlib import Path

from opal_sync.config import load_config, save_config


def test_load_config_reads_include_exclude_and_course_paths(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[general]
sync_directory = "downloads"

[sync]
include_extensions = [".PDF", "tar.gz"]
exclude_extensions = [".tmp"]

[courses]
include = ["Math"]
exclude = ["Archive"]

[courses.paths]
"Mathematics" = "Studies/Math"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.courses.include == ["Math"]
    assert config.courses.exclude == ["Archive"]
    assert config.courses.paths == {"Mathematics": "Studies/Math"}
    assert config.sync.include_extensions == [".pdf", "tar.gz"]
    assert config.sync.exclude_extensions == [".tmp"]
    assert config.general.sync_directory == (tmp_path / "downloads").resolve()


def test_load_config_uses_include_exclude_filters(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[courses]
include = ["Math"]
exclude = ["Archive"]
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.courses.include == ["Math"]
    assert config.courses.exclude == ["Archive"]


def test_save_config_persists_course_path_overrides(tmp_path):
    config_path = tmp_path / "config.toml"
    config = load_config(config_path)
    config.courses.include = ["Math"]
    config.courses.exclude = ["Archive"]
    config.courses.paths = {"Mathematics": "Studies/Math"}

    save_config(config)
    loaded = load_config(config_path)

    assert loaded.courses.include == ["Math"]
    assert loaded.courses.exclude == ["Archive"]
    assert loaded.courses.paths == {"Mathematics": "Studies/Math"}


def test_legacy_file_extensions_no_longer_limits_default_sync(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[sync]
file_extensions = [".pdf"]
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.sync.include_extensions == []
    assert config.sync.exclude_extensions == []


def test_default_config_is_in_current_directory_and_uses_relative_path_defaults(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.config_file_path == tmp_path / "config.toml"
    assert config.general.sync_directory == tmp_path
    assert config.general.log_level == "INFO"
    assert config.general.log_file == tmp_path / "opal-sync.log"
    assert config.general.db_file == tmp_path / "state.db"
    assert config.db_path == tmp_path / "state.db"
    assert config.auth.session.cookie_file == tmp_path / "session.json"


def test_config_relative_paths_resolve_from_config_directory(tmp_path):
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text(
        """
[general]
sync_directory = "."
log_file = "logs/opal.log"
db_file = "data/state.db"

[auth.session]
cookie_file = "auth/session.json"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.general.sync_directory == config_dir
    assert config.general.log_file == config_dir / "logs" / "opal.log"
    assert config.general.db_file == config_dir / "data" / "state.db"
    assert config.db_path == config_dir / "data" / "state.db"
    assert config.auth.session.cookie_file == config_dir / "auth" / "session.json"


def test_existing_config_uses_relative_path_defaults_for_omitted_values(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[general]
sync_directory = "."
log_level = "INFO"

[auth.session]
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.general.sync_directory == tmp_path
    assert config.general.log_level == "INFO"
    assert config.general.log_file == tmp_path / "opal-sync.log"
    assert config.general.db_file == tmp_path / "state.db"
    assert config.auth.session.cookie_file == tmp_path / "session.json"


def test_legacy_interval_setting_is_ignored_and_not_saved(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[sync]
interval_minutes = 60
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config)

    assert not hasattr(config.sync, "interval_minutes")
    assert "interval_minutes" not in config_path.read_text(encoding="utf-8")
