import sqlite3
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from opal_sync.models.course import Course
from opal_sync.models.node import CourseNode
from opal_sync.models.file_entry import FileEntry


class Database:
    """SQLite database manager for sync state and tracking."""

    def __init__(self, db_path: Path, sync_directory: Optional[Path] = None):
        self.db_path = Path(db_path).expanduser().resolve()
        self.sync_directory = Path(
            sync_directory or self.db_path.parent
        ).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def set_sync_directory(self, sync_directory: Path) -> None:
        previous_directory = self.sync_directory
        self.sync_directory = Path(sync_directory).expanduser().resolve()
        with self.get_connection() as conn:
            if previous_directory != self.sync_directory:
                rows = conn.execute("SELECT id, local_path FROM files").fetchall()
                for row in rows:
                    old_path = Path(row["local_path"])
                    absolute_path = (
                        old_path
                        if old_path.is_absolute()
                        else previous_directory / old_path
                    )
                    conn.execute(
                        "UPDATE files SET local_path = ? WHERE id = ?",
                        (
                            self._to_stored_local_path(str(absolute_path)),
                            row["id"],
                        ),
                    )
            else:
                self._migrate_local_paths(conn)

    def _to_stored_local_path(self, local_path: str) -> str:
        path = Path(local_path).expanduser()
        if not path.is_absolute():
            return path.as_posix()
        try:
            return Path(os.path.relpath(path.resolve(), self.sync_directory)).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"File path '{local_path}' is on a different drive than the sync directory "
                f"'{self.sync_directory}' and cannot be stored as a relative path"
            ) from exc

    def _from_stored_local_path(self, local_path: str) -> str:
        path = Path(local_path)
        if path.is_absolute():
            return str(path)
        return str((self.sync_directory / path).resolve())

    def _migrate_local_paths(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, local_path FROM files").fetchall()
        for row in rows:
            local_path = row["local_path"]
            if Path(local_path).is_absolute():
                conn.execute(
                    "UPDATE files SET local_path = ? WHERE id = ?",
                    (self._to_stored_local_path(local_path), row["id"]),
                )

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        """Create tables according to the architecture specification."""
        with self.get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS courses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                opal_id         TEXT NOT NULL UNIQUE,
                name            TEXT NOT NULL,
                url             TEXT NOT NULL,
                semester        TEXT,
                last_synced_at  DATETIME,
                is_excluded     BOOLEAN DEFAULT 0,
                discovered_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS course_nodes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id       INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                node_id         TEXT,
                node_type       TEXT DEFAULT 'folder',
                title           TEXT,
                parent_node_id  INTEGER REFERENCES course_nodes(id),
                url             TEXT,
                UNIQUE(course_id, node_id)
            );

            CREATE TABLE IF NOT EXISTS files (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id       INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                node_id         INTEGER REFERENCES course_nodes(id),
                remote_url      TEXT NOT NULL,
                remote_path     TEXT,
                local_path      TEXT NOT NULL,
                filename        TEXT NOT NULL,
                extension       TEXT,
                size_bytes      INTEGER,
                content_type    TEXT,
                etag            TEXT,
                last_modified   TEXT,
                downloaded_at   DATETIME NOT NULL,
                is_removed      BOOLEAN NOT NULL DEFAULT 0,
                UNIQUE(course_id, remote_url)
            );

            CREATE TABLE IF NOT EXISTS folder_state (
                course_id       INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                remote_url      TEXT NOT NULL,
                remote_path     TEXT NOT NULL,
                last_modified   TEXT NOT NULL,
                PRIMARY KEY (course_id, remote_url)
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at      DATETIME NOT NULL,
                finished_at     DATETIME,
                status          TEXT NOT NULL DEFAULT 'running',
                files_new       INTEGER DEFAULT 0,
                files_updated   INTEGER DEFAULT 0,
                files_skipped   INTEGER DEFAULT 0,
                files_failed    INTEGER DEFAULT 0,
                bytes_downloaded INTEGER DEFAULT 0,
                error_message   TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER REFERENCES sync_runs(id),
                timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP,
                level           TEXT NOT NULL,
                action          TEXT NOT NULL,
                course_name     TEXT,
                file_path       TEXT,
                details         TEXT
            );
            """)
            file_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(files)")
            }
            if "checksum_sha256" in file_columns:
                conn.execute("ALTER TABLE files DROP COLUMN checksum_sha256")
            if "is_removed" not in file_columns:
                conn.execute(
                    "ALTER TABLE files ADD COLUMN is_removed BOOLEAN NOT NULL DEFAULT 0"
                )
            self._migrate_local_paths(conn)

    # Course operations
    def upsert_course(self, course: Course) -> Course:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO courses (opal_id, name, url, semester, is_excluded)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(opal_id) DO UPDATE SET
                    name = excluded.name,
                    url = excluded.url,
                    semester = COALESCE(excluded.semester, courses.semester)
                RETURNING id, discovered_at, last_synced_at, is_excluded;
            """, (course.opal_id, course.name, course.url, course.semester, course.is_excluded))
            row = cursor.fetchone()
            course.id = row["id"]
            if row["discovered_at"]:
                course.discovered_at = datetime.fromisoformat(row["discovered_at"])
            if row["last_synced_at"]:
                course.last_synced_at = datetime.fromisoformat(row["last_synced_at"])
            course.is_excluded = bool(row["is_excluded"])
            return course

    def get_courses(self, include_excluded: bool = True) -> List[Course]:
        query = "SELECT * FROM courses"
        if not include_excluded:
            query += " WHERE is_excluded = 0"
        query += " ORDER BY name COLLATE NOCASE ASC"

        with self.get_connection() as conn:
            rows = conn.execute(query).fetchall()
            courses = []
            for r in rows:
                c = Course(
                    id=r["id"],
                    opal_id=r["opal_id"],
                    name=r["name"],
                    url=r["url"],
                    semester=r["semester"],
                    last_synced_at=datetime.fromisoformat(r["last_synced_at"]) if r["last_synced_at"] else None,
                    is_excluded=bool(r["is_excluded"]),
                    discovered_at=datetime.fromisoformat(r["discovered_at"]) if r["discovered_at"] else None,
                )
                courses.append(c)
            return courses

    def mark_course_synced(self, course_id: int) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE courses SET last_synced_at = CURRENT_TIMESTAMP WHERE id = ?",
                (course_id,)
            )

    def get_folder_states(self, course_id: int) -> Dict[str, tuple[str, str]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT remote_url, remote_path, last_modified
                FROM folder_state
                WHERE course_id = ?
                """,
                (course_id,),
            ).fetchall()
            return {
                row["remote_url"]: (row["remote_path"], row["last_modified"])
                for row in rows
            }

    def upsert_folder_state(
        self,
        course_id: int,
        remote_url: str,
        remote_path: str,
        last_modified: str,
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO folder_state (
                    course_id, remote_url, remote_path, last_modified
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(course_id, remote_url) DO UPDATE SET
                    remote_path = excluded.remote_path,
                    last_modified = excluded.last_modified
                """,
                (course_id, remote_url, remote_path, last_modified),
            )

    # Node operations
    def upsert_node(self, node: CourseNode) -> CourseNode:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO course_nodes (course_id, node_id, node_type, title, parent_node_id, url)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_id, node_id) DO UPDATE SET
                    title = excluded.title,
                    node_type = excluded.node_type,
                    url = excluded.url
                RETURNING id;
            """, (node.course_id, node.node_id, node.node_type, node.title, node.parent_node_id, node.url))
            row = cursor.fetchone()
            node.id = row["id"]
            return node

    # File operations
    def get_file(self, course_id: int, remote_url: str) -> Optional[FileEntry]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE course_id = ? AND remote_url = ?",
                (course_id, remote_url)
            ).fetchone()
            if not row:
                return None
            return self._file_entry_from_row(row)

    def get_file_by_local_path(self, course_id: int, local_path: str) -> Optional[FileEntry]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE course_id = ? AND local_path = ?",
                (course_id, self._to_stored_local_path(local_path))
            ).fetchone()
            return self._file_entry_from_row(row) if row else None

    def _file_entry_from_row(self, row: sqlite3.Row) -> FileEntry:
        return FileEntry(
            id=row["id"],
            course_id=row["course_id"],
            node_id=row["node_id"],
            remote_url=row["remote_url"],
            remote_path=row["remote_path"],
            local_path=self._from_stored_local_path(row["local_path"]),
            filename=row["filename"],
            extension=row["extension"],
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            downloaded_at=datetime.fromisoformat(row["downloaded_at"]) if row["downloaded_at"] else None,
            is_removed=bool(row["is_removed"]),
        )

    def upsert_file(self, file_entry: FileEntry) -> FileEntry:
        now = file_entry.downloaded_at or datetime.utcnow()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO files (
                    course_id, node_id, remote_url, remote_path, local_path,
                    filename, extension, size_bytes, content_type, etag,
                    last_modified, downloaded_at, is_removed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(course_id, remote_url) DO UPDATE SET
                    node_id = excluded.node_id,
                    remote_path = excluded.remote_path,
                    local_path = excluded.local_path,
                    filename = excluded.filename,
                    extension = excluded.extension,
                    size_bytes = excluded.size_bytes,
                    content_type = excluded.content_type,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    downloaded_at = excluded.downloaded_at,
                    is_removed = 0
                RETURNING id;
            """, (
                file_entry.course_id, file_entry.node_id, file_entry.remote_url,
                file_entry.remote_path,
                self._to_stored_local_path(file_entry.local_path),
                file_entry.filename,
                file_entry.extension, file_entry.size_bytes, file_entry.content_type,
                file_entry.etag, file_entry.last_modified, now.isoformat()
            ))
            row = cursor.fetchone()
            file_entry.id = row["id"]
            file_entry.downloaded_at = now
            return file_entry

    def mark_file_present(self, course_id: int, remote_url: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE files SET is_removed = 0 WHERE course_id = ? AND remote_url = ?",
                (course_id, remote_url),
            )

    def mark_file_removed(self, course_id: int, remote_url: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE files SET is_removed = 1
                WHERE course_id = ? AND remote_url = ? AND is_removed = 0
                """,
                (course_id, remote_url),
            )
            return cursor.rowcount > 0

    def update_file_listing_metadata(
        self,
        course_id: int,
        remote_url: str,
        remote_path: Optional[str],
        size_bytes: Optional[int],
        last_modified: Optional[str],
        local_path: Optional[str] = None,
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE files
                SET remote_path = ?,
                    size_bytes = COALESCE(?, size_bytes),
                    last_modified = COALESCE(?, last_modified),
                    local_path = COALESCE(?, local_path)
                WHERE course_id = ? AND remote_url = ?
                """,
                (
                    remote_path,
                    size_bytes,
                    last_modified,
                    self._to_stored_local_path(local_path) if local_path else None,
                    course_id,
                    remote_url,
                ),
            )

    def get_course_files(self, course_id: int) -> List[FileEntry]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM files WHERE course_id = ?", (course_id,)).fetchall()
            return [
                FileEntry(
                    id=r["id"],
                    course_id=r["course_id"],
                    node_id=r["node_id"],
                    remote_url=r["remote_url"],
                    remote_path=r["remote_path"],
                    local_path=self._from_stored_local_path(r["local_path"]),
                    filename=r["filename"],
                    extension=r["extension"],
                    size_bytes=r["size_bytes"],
                    content_type=r["content_type"],
                    etag=r["etag"],
                    last_modified=r["last_modified"],
                    downloaded_at=datetime.fromisoformat(r["downloaded_at"]) if r["downloaded_at"] else None,
                    is_removed=bool(r["is_removed"]),
                ) for r in rows
            ]

    # Sync run & log operations
    def start_sync_run(self) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO sync_runs (started_at, status) VALUES (CURRENT_TIMESTAMP, 'running') RETURNING id;"
            )
            return cursor.fetchone()["id"]

    def finish_sync_run(
        self,
        run_id: int,
        status: str,
        files_new: int,
        files_updated: int,
        files_skipped: int,
        files_failed: int,
        bytes_downloaded: int,
        error_message: Optional[str] = None
    ) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE sync_runs SET
                    finished_at = CURRENT_TIMESTAMP,
                    status = ?,
                    files_new = ?,
                    files_updated = ?,
                    files_skipped = ?,
                    files_failed = ?,
                    bytes_downloaded = ?,
                    error_message = ?
                WHERE id = ?;
            """, (status, files_new, files_updated, files_skipped, files_failed, bytes_downloaded, error_message, run_id))

    def log_action(
        self,
        run_id: Optional[int],
        level: str,
        action: str,
        course_name: Optional[str] = None,
        file_path: Optional[str] = None,
        details: Optional[str] = None
    ) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO sync_log (run_id, level, action, course_name, file_path, details)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (run_id, level.lower(), action, course_name, file_path, details))

    def get_recent_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_logs(self, limit: int = 50, only_errors: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM sync_log"
        params: List[Any] = []
        if only_errors:
            query += " WHERE level IN ('error', 'warning')"
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            total_courses = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
            total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            total_bytes = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM files").fetchone()[0]
            last_run = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
            return {
                "total_courses": total_courses,
                "total_files": total_files,
                "total_bytes": total_bytes,
                "last_run": dict(last_run) if last_run else None,
            }
