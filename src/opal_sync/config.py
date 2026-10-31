import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore

import tomli_w


DEFAULT_CONFIG_PATH = Path("config.toml")

UNIVERSITY_MAP: Dict[str, int] = {
    "berufsakademie-sachsen": 0,
    "duale-hochschule": 1,
    "hfm-dresden": 2,
    "hochschuldidaktik": 3,
    "hs-mittweida": 4,
    "hs-zittau-goerlitz": 5,
    "htw-dresden": 6,
    "htwk-leipzig": 7,
    "slub-dresden": 8,
    "svwa": 9,
    "smwkt": 10,
    "tu-freiberg": 11,
    "tu-chemnitz": 12,
    "tu-dresden": 13,
    "uni-leipzig": 14,
    "whz-zwickau": 15,
    "palucca-dresden": 16,
    "bps-vho": 17,
}


@dataclass
class GeneralConfig:
    sync_directory: Path = Path(".")
    log_level: str = "INFO"
    log_file: Optional[Path] = Path("opal-sync.log")
    db_file: Optional[Path] = Path("state.db")


@dataclass
class SessionConfig:
    cookie_file: Path = Path("session.json")
    session_ttl_hours: int = 1
    max_retry_auth: int = 3


@dataclass
class AuthConfig:
    university: str = "htwk-leipzig"
    username: str = ""
    password: Optional[str] = None  # For dev/testing; prefer keyring in production
    session: SessionConfig = field(default_factory=SessionConfig)

    @property
    def wayf_value(self) -> int:
        normalized = self.university.lower().strip()
        if normalized not in UNIVERSITY_MAP:
            # If a digit was entered directly
            if normalized.isdigit():
                return int(normalized)
            raise ValueError(f"Unknown university '{self.university}'. Available: {list(UNIVERSITY_MAP.keys())}")
        return UNIVERSITY_MAP[normalized]


@dataclass
class SyncConfig:
    include_extensions: List[str] = field(default_factory=list)
    exclude_extensions: List[str] = field(default_factory=list)
    max_file_size_mb: int = 50
    parallel_downloads: int = 3
    request_delay_seconds: float = 0.5
    delete_removed: bool = False


@dataclass
class CoursesFilterConfig:
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    paths: Dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    courses: CoursesFilterConfig = field(default_factory=CoursesFilterConfig)
    config_file_path: Optional[Path] = None

    @property
    def db_path(self) -> Path:
        if self.general.db_file:
            db_file = self.general.db_file.expanduser()
            if db_file.is_absolute():
                return db_file
            base_dir = (
                self.config_file_path.parent
                if self.config_file_path
                else Path.cwd()
            )
            return (base_dir / db_file).resolve()
        base_dir = self.config_file_path.parent if self.config_file_path else Path.cwd()
        default_db_file = GeneralConfig().db_file
        assert default_db_file is not None
        return (base_dir / default_db_file).resolve()


def _resolve_config_path(value: Any, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _path_for_config_file(value: Optional[Path], base_dir: Path) -> str:
    if value is None:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    """Load configuration from a TOML file or return defaults."""
    path = config_path or (Path.cwd() / DEFAULT_CONFIG_PATH)
    path = Path(path).expanduser().resolve()
    base_dir = path.parent

    if not path.is_file():
        cfg = AppConfig()
        cfg.config_file_path = path
        cfg.general.sync_directory = _resolve_config_path(
            cfg.general.sync_directory, base_dir
        )
        if cfg.general.log_file is not None:
            cfg.general.log_file = _resolve_config_path(cfg.general.log_file, base_dir)
        if cfg.general.db_file is not None:
            cfg.general.db_file = _resolve_config_path(cfg.general.db_file, base_dir)
        cfg.auth.session.cookie_file = _resolve_config_path(
            cfg.auth.session.cookie_file, base_dir
        )
        return cfg

    with open(path, "rb") as f:
        data: Dict[str, Any] = tomllib.load(f)

    gen_raw = data.get("general", {})
    general_defaults = GeneralConfig()
    log_file_raw = gen_raw.get("log_file", general_defaults.log_file)
    if log_file_raw is None:
        log_file_raw = general_defaults.log_file
    elif isinstance(log_file_raw, str) and not log_file_raw.strip():
        log_file_raw = None

    db_file_raw = gen_raw.get("db_file", general_defaults.db_file)
    if db_file_raw is None:
        db_file_raw = general_defaults.db_file
    elif isinstance(db_file_raw, str) and not db_file_raw.strip():
        db_file_raw = None

    general = GeneralConfig(
        sync_directory=_resolve_config_path(
            gen_raw.get("sync_directory", general_defaults.sync_directory), base_dir
        ),
        log_level=gen_raw.get("log_level", general_defaults.log_level),
        log_file=(
            _resolve_config_path(log_file_raw, base_dir)
            if log_file_raw not in (None, "")
            else None
        ),
        db_file=(
            _resolve_config_path(db_file_raw, base_dir)
            if db_file_raw not in (None, "")
            else None
        ),
    )

    auth_raw = data.get("auth", {})
    sess_raw = auth_raw.get("session", {})
    auth_defaults = AuthConfig()
    session_defaults = auth_defaults.session
    session = SessionConfig(
        cookie_file=_resolve_config_path(
            sess_raw.get("cookie_file", session_defaults.cookie_file),
            base_dir,
        ),
        session_ttl_hours=int(
            sess_raw.get("session_ttl_hours", session_defaults.session_ttl_hours)
        ),
        max_retry_auth=int(
            sess_raw.get("max_retry_auth", session_defaults.max_retry_auth)
        ),
    )
    auth = AuthConfig(
        university=auth_raw.get("university", auth_defaults.university),
        username=auth_raw.get("username", auth_defaults.username),
        password=auth_raw.get("password", auth_defaults.password),
        session=session,
    )

    sync_raw = data.get("sync", {})
    sync_defaults = SyncConfig()
    sync = SyncConfig(
        include_extensions=[
            ext.lower()
            for ext in sync_raw.get(
                "include_extensions", sync_defaults.include_extensions
            )
        ],
        exclude_extensions=[
            ext.lower()
            for ext in sync_raw.get(
                "exclude_extensions", sync_defaults.exclude_extensions
            )
        ],
        max_file_size_mb=int(
            sync_raw.get("max_file_size_mb", sync_defaults.max_file_size_mb)
        ),
        parallel_downloads=int(
            sync_raw.get("parallel_downloads", sync_defaults.parallel_downloads)
        ),
        request_delay_seconds=float(
            sync_raw.get("request_delay_seconds", sync_defaults.request_delay_seconds)
        ),
        delete_removed=bool(
            sync_raw.get("delete_removed", sync_defaults.delete_removed)
        ),
    )

    courses_raw = data.get("courses", {})
    courses_defaults = CoursesFilterConfig()
    courses = CoursesFilterConfig(
        include=courses_raw.get("include", courses_defaults.include),
        exclude=courses_raw.get("exclude", courses_defaults.exclude),
        paths={
            str(course_name): str(course_path)
            for course_name, course_path in courses_raw.get(
                "paths", courses_defaults.paths
            ).items()
        },
    )

    return AppConfig(
        general=general,
        auth=auth,
        sync=sync,
        courses=courses,
        config_file_path=path,
    )


def save_config(config: AppConfig, path: Optional[Path] = None) -> Path:
    """Save configuration to TOML file."""
    target_path = Path(
        path or config.config_file_path or (Path.cwd() / DEFAULT_CONFIG_PATH)
    ).expanduser().resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = target_path.parent

    data: Dict[str, Any] = {
        "general": {
            "sync_directory": _path_for_config_file(
                config.general.sync_directory, base_dir
            ),
            "log_level": config.general.log_level,
            "log_file": _path_for_config_file(config.general.log_file, base_dir),
            "db_file": _path_for_config_file(config.general.db_file, base_dir),
        },
        "auth": {
            "university": config.auth.university,
            "username": config.auth.username,
            "session": {
                "cookie_file": _path_for_config_file(
                    config.auth.session.cookie_file, base_dir
                ),
                "session_ttl_hours": config.auth.session.session_ttl_hours,
                "max_retry_auth": config.auth.session.max_retry_auth,
            },
        },
        "sync": {
            "include_extensions": config.sync.include_extensions,
            "exclude_extensions": config.sync.exclude_extensions,
            "max_file_size_mb": config.sync.max_file_size_mb,
            "parallel_downloads": config.sync.parallel_downloads,
            "request_delay_seconds": config.sync.request_delay_seconds,
            "delete_removed": config.sync.delete_removed,
        },
        "courses": {
            "include": config.courses.include,
            "exclude": config.courses.exclude,
            "paths": config.courses.paths,
        },
    }

    with open(target_path, "wb") as f:
        tomli_w.dump(data, f)

    return target_path
