from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Course:
    """Represents an enrolled course in OPAL."""
    opal_id: str                      # RepositoryEntry ID from URL
    name: str                         # Course display title
    url: str                          # Full URL to the course
    id: Optional[int] = None          # Internal SQLite row ID
    semester: Optional[str] = None    # e.g., "WS 2026/27"
    last_synced_at: Optional[datetime] = None
    is_excluded: bool = False
    discovered_at: Optional[datetime] = None
