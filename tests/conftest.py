from pathlib import Path
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def shiblogin_html(fixtures_dir) -> str:
    return (fixtures_dir / "shiblogin.html").read_text(encoding="utf-8")


@pytest.fixture
def idp_login_html(fixtures_dir) -> str:
    return (fixtures_dir / "idp_login.html").read_text(encoding="utf-8")


@pytest.fixture
def idp_creds_html(fixtures_dir) -> str:
    return (fixtures_dir / "idp_creds.html").read_text(encoding="utf-8")


@pytest.fixture
def my_courses_html(fixtures_dir) -> str:
    return (fixtures_dir / "my_courses.html").read_text(encoding="utf-8")


@pytest.fixture
def course_folder_html(fixtures_dir) -> str:
    return (fixtures_dir / "course_folder.html").read_text(encoding="utf-8")
