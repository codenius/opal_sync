from pathlib import Path
from typing import Iterable, Optional


def normalize_extensions(extensions: Optional[Iterable[str]]) -> tuple[str, ...]:
    if not extensions:
        return ()
    return tuple(
        sorted(
            {
                extension.lower()
                if extension.startswith(".")
                else f".{extension.lower()}"
                for extension in extensions
                if extension
            },
            key=len,
            reverse=True,
        )
    )


def get_file_extension(filename: str, known_extensions: Iterable[str] = ()) -> str:
    lowered_name = Path(filename).name.lower()
    for extension in normalize_extensions(known_extensions):
        if lowered_name.endswith(extension):
            return extension
    return Path(lowered_name).suffix


def is_extension_allowed(
    filename: str,
    include_extensions: Optional[Iterable[str]] = None,
    exclude_extensions: Optional[Iterable[str]] = None,
) -> bool:
    included = normalize_extensions(include_extensions)
    excluded = normalize_extensions(exclude_extensions)
    extension = get_file_extension(filename, (*included, *excluded))
    include_match = bool(extension and any(
        Path(filename).name.lower().endswith(candidate) for candidate in included
    ))

    if included and not include_match:
        return False
    if extension and any(
        Path(filename).name.lower().endswith(candidate) for candidate in excluded
    ):
        return include_match
    return True
