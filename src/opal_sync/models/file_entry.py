from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional


@dataclass
class DownloadRequest:
    """Request details for files that require a form or AJAX download action."""
    url: str
    data: Dict[str, str]
    headers: Dict[str, str]
    method: str = "POST"


@dataclass
class FileEntry:
    """Represents a file discovered within an OPAL course node."""
    course_id: int
    remote_url: str
    local_path: str
    filename: str
    id: Optional[int] = None
    node_id: Optional[int] = None
    remote_path: Optional[str] = None
    extension: Optional[str] = None
    size_bytes: Optional[int] = None
    content_type: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    downloaded_at: Optional[datetime] = None
    is_removed: bool = False
    download_request: Optional[DownloadRequest] = None
