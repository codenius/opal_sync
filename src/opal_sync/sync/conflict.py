from pathlib import Path
from typing import Optional

from opal_sync.models.file_entry import FileEntry
from opal_sync.utils.logging import get_logger

logger = get_logger()


def resolve_filename_conflict(
    target_path: Path,
    remote_url: str,
    db_file_for_path: Optional[FileEntry] = None,
) -> Path:
    """Resolve filename collisions when multiple distinct remote files map to the same local name.

    If the target path exists and belongs to a different remote URL, appends an incrementing counter:
    'name-1.ext', 'name-2.ext', etc.
    """
    if not target_path.exists():
        return target_path

    # If this path already belongs to the same remote URL, it's not a collision
    if db_file_for_path and db_file_for_path.remote_url == remote_url:
        return target_path

    parent = target_path.parent
    stem = target_path.stem
    ext = target_path.suffix

    counter = 1
    candidate = parent / f"{stem}-{counter}{ext}"
    while candidate.exists():
        # If candidate belongs to the exact same URL, we can reuse it
        if db_file_for_path and db_file_for_path.remote_url == remote_url and db_file_for_path.local_path == str(candidate):
            return candidate
        counter += 1
        candidate = parent / f"{stem}-{counter}{ext}"

    logger.debug(f"Resolved filename conflict for '{target_path.name}' -> '{candidate.name}'")
    return candidate


def next_version_path(target_path: Path) -> Path:
    """Return the next unused '-N' filename while preserving existing versions."""
    parent = target_path.parent
    stem = target_path.stem
    ext = target_path.suffix
    counter = 1
    candidate = parent / f"{stem}-{counter}{ext}"
    while candidate.exists():
        counter += 1
        candidate = parent / f"{stem}-{counter}{ext}"
    return candidate
