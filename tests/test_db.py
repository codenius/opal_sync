from datetime import datetime, timedelta
from opal_sync.models.course import Course
from opal_sync.models.file_entry import FileEntry
from opal_sync.models.node import CourseNode
from opal_sync.storage.db import Database


def test_database_crud(tmp_path):
    db_file = tmp_path / "test_state.db"
    db = Database(db_file)

    # 1. Course upsert
    course = Course(
        opal_id="999888",
        name="Datenbanksysteme 1",
        url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/999888",
        semester="WS 2026/27",
    )
    saved_course = db.upsert_course(course)
    assert saved_course.id is not None
    assert saved_course.id > 0

    courses = db.get_courses()
    assert len(courses) == 1
    assert courses[0].name == "Datenbanksysteme 1"

    # 2. Node upsert
    node = CourseNode(
        course_id=saved_course.id,
        node_id="node_001",
        title="Vorlesungen",
        node_type="folder",
        url="https://bildungsportal.sachsen.de/node_001",
    )
    saved_node = db.upsert_node(node)
    assert saved_node.id is not None

    db.upsert_folder_state(
        course_id=saved_course.id,
        remote_url="https://bildungsportal.sachsen.de/node_001/Teilordner",
        remote_path="Vorlesungen/Teilordner",
        last_modified="am 27.10.2026 um 12:44 Uhr",
    )
    assert db.get_folder_states(saved_course.id) == {
        "https://bildungsportal.sachsen.de/node_001/Teilordner": (
            "Vorlesungen/Teilordner",
            "am 27.10.2026 um 12:44 Uhr",
        )
    }

    # 3. File upsert
    file_entry = FileEntry(
        course_id=saved_course.id,
        node_id=saved_node.id,
        remote_url="https://bildungsportal.sachsen.de/files/db_script.pdf",
        remote_path="Vorlesungen/db_script.pdf",
        local_path=str(tmp_path / "db_script.pdf"),
        filename="db_script.pdf",
        extension=".pdf",
        size_bytes=1048576,
        etag='"etag123"',
    )
    saved_file = db.upsert_file(file_entry)
    assert saved_file.id is not None

    fetched_file = db.get_file(saved_course.id, "https://bildungsportal.sachsen.de/files/db_script.pdf")
    assert fetched_file is not None
    assert fetched_file.filename == "db_script.pdf"
    assert fetched_file.size_bytes == 1048576
    with db.get_connection() as conn:
        stored_path = conn.execute(
            "SELECT local_path FROM files WHERE id = ?", (saved_file.id,)
        ).fetchone()["local_path"]
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(files)")
        }
    assert stored_path == "db_script.pdf"
    assert "checksum_sha256" not in columns
    same_path_file = db.get_file_by_local_path(
        saved_course.id,
        str(tmp_path / "db_script.pdf"),
    )
    assert same_path_file is not None
    assert same_path_file.remote_path == "Vorlesungen/db_script.pdf"
    assert fetched_file.local_path == str(tmp_path / "db_script.pdf")
    assert db.mark_file_removed(
        saved_course.id, "https://bildungsportal.sachsen.de/files/db_script.pdf"
    )
    assert db.get_file(
        saved_course.id, "https://bildungsportal.sachsen.de/files/db_script.pdf"
    ).is_removed
    db.mark_file_present(
        saved_course.id, "https://bildungsportal.sachsen.de/files/db_script.pdf"
    )
    assert not db.get_file(
        saved_course.id, "https://bildungsportal.sachsen.de/files/db_script.pdf"
    ).is_removed

    # 4. Sync runs
    run_id = db.start_sync_run()
    assert run_id > 0

    db.finish_sync_run(
        run_id=run_id,
        status="success",
        files_new=1,
        files_updated=0,
        files_skipped=2,
        files_failed=0,
        bytes_downloaded=1048576,
    )

    runs = db.get_recent_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["files_new"] == 1

    recent_downloads = db.get_recent_downloads()
    assert len(recent_downloads) == 1
    assert recent_downloads[0]["filename"] == "db_script.pdf"
    assert recent_downloads[0]["local_path"] == "db_script.pdf"

    # 5. Stats
    stats = db.get_stats()
    assert stats["total_courses"] == 1
    assert stats["total_files"] == 1
    assert stats["total_bytes"] == 1048576


def test_database_migrates_absolute_paths_and_removes_checksums(tmp_path):
    db_file = tmp_path / "legacy.db"
    sync_directory = tmp_path / "sync"
    db = Database(db_file, sync_directory=sync_directory)
    course = db.upsert_course(
        Course(
            opal_id="123",
            name="Course",
            url="https://example.com/course",
        )
    )
    file_entry = FileEntry(
        course_id=course.id,
        remote_url="https://example.com/file.pdf",
        local_path=str(sync_directory / "Course" / "file.pdf"),
        filename="file.pdf",
    )
    saved_file = db.upsert_file(file_entry)
    with db.get_connection() as conn:
        conn.execute("ALTER TABLE files ADD COLUMN checksum_sha256 TEXT")
        conn.execute(
            "UPDATE files SET local_path = ?, checksum_sha256 = ? WHERE id = ?",
            (str(sync_directory / "Course" / "file.pdf"), "legacy", saved_file.id),
        )

    db.init_schema()

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT local_path FROM files WHERE id = ?", (saved_file.id,)
        ).fetchone()
        columns = {
            column["name"] for column in conn.execute("PRAGMA table_info(files)")
        }
    assert row["local_path"] == "Course/file.pdf"
    assert "checksum_sha256" not in columns
    migrated_file = db.get_file(course.id, file_entry.remote_url)
    assert migrated_file is not None
    assert migrated_file.local_path == str(sync_directory / "Course" / "file.pdf")


def test_recent_downloads_are_ordered_and_include_removed_files(tmp_path):
    db = Database(tmp_path / "recent.db", sync_directory=tmp_path)
    course = db.upsert_course(
        Course(opal_id="recent", name="Course", url="https://example.com/course")
    )

    for index in range(12):
        db.upsert_file(
            FileEntry(
                course_id=course.id,
                remote_url=f"https://example.com/{index}.pdf",
                local_path=str(tmp_path / "Course" / f"{index}.pdf"),
                filename=f"{index}.pdf",
                last_modified=f"Nov {index + 1}, 2026",
                downloaded_at=datetime(2026, 1, 1) + timedelta(minutes=index),
            )
        )

    db.mark_file_removed(course.id, "https://example.com/11.pdf")

    recent = db.get_recent_downloads()
    assert [entry["filename"] for entry in recent] == [
        "11.pdf",
        "10.pdf",
        "9.pdf",
        "8.pdf",
        "7.pdf",
        "6.pdf",
        "5.pdf",
        "4.pdf",
        "3.pdf",
        "2.pdf",
    ]
    assert recent[0]["is_removed"]
    assert recent[0]["local_path"] == "Course/11.pdf"
    assert recent[0]["last_modified"] == "Nov 12, 2026"
    assert len(db.get_recent_downloads(limit=1)) == 1


def test_database_drops_legacy_sync_log_table(tmp_path):
    db = Database(tmp_path / "legacy-log.db")
    with db.get_connection() as conn:
        conn.execute("""
            CREATE TABLE sync_log (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                timestamp DATETIME,
                level TEXT,
                action TEXT,
                course_name TEXT,
                file_path TEXT,
                details TEXT
            )
        """)
        conn.execute(
            "INSERT INTO sync_log (level, action) VALUES ('info', 'DOWNLOAD')"
        )

    db.init_schema()

    with db.get_connection() as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sync_log'"
        ).fetchone()
    assert table is None


def test_database_prunes_sync_runs_by_age_and_count(tmp_path):
    db = Database(tmp_path / "retention.db")
    old_started_at = (datetime.utcnow() - timedelta(days=31)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    recent_started_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with db.get_connection() as conn:
        for index in range(101):
            conn.execute(
                "INSERT INTO sync_runs (started_at, status) VALUES (?, ?)",
                (
                    old_started_at if index == 0 else recent_started_at,
                    "running" if index == 0 else "success",
                ),
            )
        old_run_id = 1
        oldest_recent_run_id = 2

    db.init_schema()
    runs_after_age_prune = db.get_recent_runs(limit=200)
    assert len(runs_after_age_prune) == 100
    assert old_run_id not in {run["id"] for run in runs_after_age_prune}

    run_id = db.start_sync_run()
    db.finish_sync_run(
        run_id=run_id,
        status="success",
        files_new=0,
        files_updated=0,
        files_skipped=0,
        files_failed=0,
        bytes_downloaded=0,
    )
    retained_run_ids = {run["id"] for run in db.get_recent_runs(limit=200)}
    assert len(retained_run_ids) == 100
    assert oldest_recent_run_id not in retained_run_ids
    assert run_id in retained_run_ids
