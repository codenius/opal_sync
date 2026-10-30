from datetime import datetime
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

    # 4. Sync runs & logs
    run_id = db.start_sync_run()
    assert run_id > 0

    db.log_action(
        run_id=run_id,
        level="INFO",
        action="DOWNLOAD",
        course_name=saved_course.name,
        details="Downloaded script",
    )

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

    logs = db.get_recent_logs()
    assert len(logs) == 1
    assert logs[0]["action"] == "DOWNLOAD"

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
