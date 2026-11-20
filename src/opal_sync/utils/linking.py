from pathlib import Path
from typing import Optional

from rich.text import Text

from opal_sync.utils.sanitize import sanitize_filename


def local_path_text(path: Path, display: Optional[str] = None) -> Text:
    resolved = path.expanduser().resolve()
    return Text(
        display if display is not None else str(path),
        style=f"link {resolved.as_uri()}",
    )


def course_directory(
    sync_directory: Path,
    course_paths: dict[str, Path],
    course_name: str,
) -> Path:
    configured_path = next(
        (
            path
            for name, path in course_paths.items()
            if name.casefold() == course_name.casefold()
        ),
        None,
    )
    if configured_path is None:
        return sync_directory / sanitize_filename(course_name)

    path = Path(configured_path).expanduser()
    if path.is_absolute():
        return path
    if ".." in path.parts:
        raise ValueError(
            f"Relative course path for '{course_name}' cannot contain '..'"
        )
    return sync_directory.joinpath(
        *(sanitize_filename(part) for part in path.parts if part not in ("", "."))
    )
