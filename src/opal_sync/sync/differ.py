from enum import Enum
from pathlib import Path
import re
from typing import List, Optional, Tuple

from opal_sync.models.file_entry import FileEntry
from opal_sync.utils.extensions import is_extension_allowed
from opal_sync.utils.logging import get_logger

logger = get_logger()


class SyncAction(str, Enum):
    DOWNLOAD_NEW = "DOWNLOAD_NEW"
    DOWNLOAD_UPDATE = "DOWNLOAD_UPDATE"
    SKIP_UNCHANGED = "SKIP_UNCHANGED"
    SKIP_EXCLUDED = "SKIP_EXCLUDED"
    SKIP_TOO_LARGE = "SKIP_TOO_LARGE"


def determine_sync_action(
    file_entry: FileEntry,
    target_local_path: Path,
    db_file: Optional[FileEntry] = None,
    allowed_extensions: Optional[List[str]] = None,
    max_file_size_bytes: Optional[int] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None,
) -> Tuple[SyncAction, str]:
    """Determine whether a file should be downloaded, updated, or skipped."""
    target_local_path = Path(target_local_path)

    # 1. Extension filter check
    include_filter = include_extensions
    if include_filter is None:
        include_filter = allowed_extensions
    if not is_extension_allowed(
        file_entry.filename or target_local_path.name,
        include_filter,
        exclude_extensions,
    ):
        return SyncAction.SKIP_EXCLUDED, "File extension is excluded by sync configuration"

    # 2. Max size check if size is already known
    if max_file_size_bytes and file_entry.size_bytes and file_entry.size_bytes > max_file_size_bytes:
        mb = file_entry.size_bytes / (1024 * 1024)
        max_mb = max_file_size_bytes / (1024 * 1024)
        return SyncAction.SKIP_TOO_LARGE, f"Size {mb:.1f}MB exceeds limit of {max_mb:.1f}MB"

    # 3. Local file does not exist
    if not target_local_path.is_file():
        if db_file is not None:
            return SyncAction.DOWNLOAD_NEW, "File was recorded in DB but missing on disk"
        return SyncAction.DOWNLOAD_NEW, "New file"

    opal_date_pattern = re.compile(r"\d{2}\.\d{2}\.\d{4}")
    current_has_opal_date = bool(
        file_entry.last_modified and opal_date_pattern.search(file_entry.last_modified)
    )
    stored_has_opal_date = bool(
        db_file
        and db_file.last_modified
        and opal_date_pattern.search(db_file.last_modified)
    )

    if db_file is not None and file_entry.last_modified and db_file.last_modified:
        if file_entry.last_modified == db_file.last_modified:
            return SyncAction.SKIP_UNCHANGED, "OPAL last-modified date is unchanged"
        if current_has_opal_date and stored_has_opal_date:
            return (
                SyncAction.DOWNLOAD_UPDATE,
                f"OPAL last-modified date changed from '{db_file.last_modified}' "
                f"to '{file_entry.last_modified}'",
            )

    if db_file is not None and current_has_opal_date and not stored_has_opal_date:
        return (
            SyncAction.SKIP_UNCHANGED,
            "Migrating to OPAL last-modified date without redownloading",
        )

    return SyncAction.SKIP_UNCHANGED, "No OPAL last-modified change detected"
