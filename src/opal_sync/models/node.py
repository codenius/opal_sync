from dataclasses import dataclass
from typing import Optional


@dataclass
class CourseNode:
    """Represents a node/element inside an OPAL course (e.g. folder, single page)."""
    course_id: int                    # References courses.id in DB
    node_id: str                      # CourseNode ID from URL
    title: str                        # Node title / folder name
    node_type: str = "folder"         # 'folder', 'singlepage', 'task', etc.
    url: Optional[str] = None         # Navigation URL to node
    id: Optional[int] = None          # Internal SQLite row ID
    parent_node_id: Optional[int] = None
    relative_path: Optional[str] = None
    last_modified: Optional[str] = None
