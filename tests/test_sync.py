from pathlib import Path
import pytest
import respx
import httpx

from opal_sync.config import AppConfig
from opal_sync.models.course import Course
from opal_sync.models.file_entry import DownloadRequest, FileEntry
from opal_sync.models.node import CourseNode
from opal_sync.storage.db import Database
from opal_sync.sync.conflict import next_version_path, resolve_filename_conflict
from opal_sync.sync.differ import SyncAction, determine_sync_action
from opal_sync.sync.downloader import FileDownloader, parse_content_disposition
from opal_sync.sync.engine import SyncEngine


def test_parse_content_disposition():
    header1 = 'attachment; filename="lecture_01.pdf"'
    assert parse_content_disposition(header1) == "lecture_01.pdf"

    header2 = "attachment; filename*=UTF-8''Formel%C3%BCbersicht.pdf"
    assert parse_content_disposition(header2) == "Formelübersicht.pdf"

    header3 = 'inline; filename="bad/name:with*illegal?.txt"'
    clean = parse_content_disposition(header3)
    assert "/" not in clean
    assert ":" not in clean


def test_exclude_filter_wins_when_course_matches_both_lists():
    config = AppConfig()
    config.courses.include = ["Mathematics"]
    config.courses.exclude = ["Mathematics"]

    class FakeDatabase:
        def set_sync_directory(self, _directory):
            pass

    engine = SyncEngine(config, db=FakeDatabase())

    assert not engine.is_course_allowed("Mathematics")


def test_determine_sync_action(tmp_path):
    target_path = tmp_path / "test.pdf"
    file_entry = FileEntry(
        course_id=1,
        remote_url="http://example.com/test.pdf",
        local_path=str(target_path),
        filename="test.pdf",
        extension=".pdf",
        size_bytes=1000,
    )

    # 1. Non-existent file
    action, reason = determine_sync_action(file_entry, target_path, allowed_extensions=[".pdf"])
    assert action == SyncAction.DOWNLOAD_NEW

    # 2. Excluded extension
    action, reason = determine_sync_action(file_entry, target_path, allowed_extensions=[".docx"])
    assert action == SyncAction.SKIP_EXCLUDED

    # 3. File exists with matching size
    target_path.write_bytes(b"%PDF-" + b"x" * 995)
    action, reason = determine_sync_action(file_entry, target_path, allowed_extensions=[".pdf"])
    assert action == SyncAction.SKIP_UNCHANGED

    target_path.write_bytes(b"<!doctype html>" + b"x" * 985)
    action, reason = determine_sync_action(file_entry, target_path, allowed_extensions=[".pdf"])
    assert action == SyncAction.SKIP_UNCHANGED
    assert "no opal last-modified change" in reason.lower()

    # Local edits do not trigger a download based on size.
    target_path.write_bytes(b"x" * 500)
    action, reason = determine_sync_action(file_entry, target_path, allowed_extensions=[".pdf"])
    assert action == SyncAction.SKIP_UNCHANGED
    assert "no opal last-modified change" in reason.lower()


def test_extension_include_overrides_exclude_and_empty_includes_allow_all(tmp_path):
    path = tmp_path / "archive.tar.gz"
    entry = FileEntry(
        course_id=1,
        remote_url="https://example.com/archive.tar.gz",
        local_path=str(path),
        filename="archive.tar.gz",
        extension=".gz",
    )
    action, _ = determine_sync_action(
        entry,
        path,
        include_extensions=[".tar.gz"],
        exclude_extensions=[".gz"],
    )
    assert action == SyncAction.DOWNLOAD_NEW

    action, _ = determine_sync_action(
        entry,
        path,
        exclude_extensions=[".gz"],
    )
    assert action == SyncAction.SKIP_EXCLUDED

    entry.filename = "model.blend"
    entry.extension = ".blend"
    action, _ = determine_sync_action(entry, tmp_path / "model.blend")
    assert action == SyncAction.DOWNLOAD_NEW


def test_determine_sync_action_uses_opal_last_modified_date(tmp_path):
    target_path = tmp_path / "test.pdf"
    target_path.write_bytes(b"%PDF-1.7\n")
    file_entry = FileEntry(
        course_id=1,
        remote_url="https://example.com/test.pdf",
        local_path=str(target_path),
        filename="test.pdf",
        extension=".pdf",
        size_bytes=100,
        last_modified="am 27.10.2026 um 12:44 Uhr",
    )
    db_file = FileEntry(
        course_id=1,
        remote_url=file_entry.remote_url,
        local_path=str(target_path),
        filename="test.pdf",
        extension=".pdf",
        size_bytes=8,
        last_modified=file_entry.last_modified,
    )

    action, _ = determine_sync_action(file_entry, target_path, db_file=db_file)
    assert action == SyncAction.SKIP_UNCHANGED

    file_entry.last_modified = "am 27.10.2026 um 12:45 Uhr"
    action, reason = determine_sync_action(file_entry, target_path, db_file=db_file)
    assert action == SyncAction.DOWNLOAD_UPDATE
    assert "OPAL last-modified date changed" in reason

    db_file.last_modified = "Tue, 27 Oct 2026 11:44:00 GMT"
    file_entry.size_bytes = target_path.stat().st_size + 1
    db_file.size_bytes = target_path.stat().st_size
    file_entry.etag = '"new-etag"'
    db_file.etag = '"old-etag"'
    action, _ = determine_sync_action(file_entry, target_path, db_file=db_file)
    assert action == SyncAction.SKIP_UNCHANGED

    db_file.size_bytes -= 1
    action, reason = determine_sync_action(file_entry, target_path, db_file=db_file)
    assert action == SyncAction.SKIP_UNCHANGED
    assert "migrating to opal last-modified date" in reason.lower()


def test_resolve_filename_conflict(tmp_path):
    file1 = tmp_path / "script.pdf"
    file1.write_text("v1")

    # Conflict with a different remote file
    resolved = resolve_filename_conflict(file1, remote_url="http://example.com/other/script.pdf")
    assert resolved.name == "script-1.pdf"

    # No conflict if same URL already in db
    db_file = FileEntry(course_id=1, remote_url="http://example.com/script.pdf", local_path=str(file1), filename="script.pdf")
    resolved_same = resolve_filename_conflict(file1, remote_url="http://example.com/script.pdf", db_file_for_path=db_file)
    assert resolved_same.name == "script.pdf"

    (tmp_path / "script-1.pdf").write_text("v2")
    assert next_version_path(file1).name == "script-2.pdf"


def test_course_exclude_list_overrides_include_list(tmp_path):
    config = AppConfig()
    config.courses.include = ["math"]
    config.courses.exclude = ["mathematics"]
    engine = SyncEngine(config=config, db=Database(tmp_path / "filters.db"))

    assert not engine.is_course_allowed("Applied Mathematics")
    assert not engine.is_course_allowed("Physics")


def test_course_exclude_list_applies_when_include_list_is_empty(tmp_path):
    config = AppConfig()
    config.courses.exclude = ["archive"]
    engine = SyncEngine(config=config, db=Database(tmp_path / "filters.db"))

    assert engine.is_course_allowed("Mathematics")
    assert not engine.is_course_allowed("Course Archive")


def test_course_root_path_defaults_to_name_and_supports_overrides(tmp_path):
    config = AppConfig()
    config.general.sync_directory = tmp_path / "sync"
    engine = SyncEngine(config=config, db=Database(tmp_path / "paths.db"))
    course = Course(
        opal_id="1",
        name="Mathematik 1",
        url="https://example.com/course",
        semester="WS 2026/27",
    )

    assert engine._course_base_dir(course) == config.general.sync_directory / "Mathematik 1"

    config.courses.paths = {"MATHEMATIK 1": "Studies/Mathematik"}
    assert engine._course_base_dir(course) == (
        config.general.sync_directory / "Studies" / "Mathematik"
    )

    config.courses.paths = {"Mathematik 1": str(tmp_path / "custom root")}
    assert engine._course_base_dir(course) == tmp_path / "custom root"


def test_tracked_file_is_moved_out_of_legacy_allgemein_folder(tmp_path):
    config = AppConfig()
    config.general.sync_directory = tmp_path / "sync"
    engine = SyncEngine(config=config, db=Database(tmp_path / "paths.db"))
    course_dir = config.general.sync_directory / "Mathematik"
    legacy_path = course_dir.parent / "Allgemein" / "Mathematik" / "Kurs" / "file.pdf"
    canonical_path = course_dir / "Kurs" / "file.pdf"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"existing file")
    db_file = FileEntry(
        course_id=1,
        remote_url="https://example.com/file.pdf",
        remote_path="Kurs/file.pdf",
        local_path=str(legacy_path),
        filename="file.pdf",
    )

    resolved = engine._resolve_tracked_file_path(
        db_file,
        "Kurs/file.pdf",
        canonical_path,
        course_dir,
    )

    assert resolved == canonical_path
    assert canonical_path.read_bytes() == b"existing file"
    assert not legacy_path.exists()


def test_tracked_version_in_course_folder_is_preserved(tmp_path):
    config = AppConfig()
    config.general.sync_directory = tmp_path / "sync"
    engine = SyncEngine(config=config, db=Database(tmp_path / "paths.db"))
    course_dir = config.general.sync_directory / "Mathematik"
    tracked_path = course_dir / "Kurs" / "file-1.pdf"
    canonical_path = course_dir / "Kurs" / "file.pdf"
    tracked_path.parent.mkdir(parents=True)
    tracked_path.write_bytes(b"previous version")
    db_file = FileEntry(
        course_id=1,
        remote_url="https://example.com/file.pdf",
        remote_path="Kurs/file.pdf",
        local_path=str(tracked_path),
        filename="file.pdf",
    )

    resolved = engine._resolve_tracked_file_path(
        db_file,
        "Kurs/file.pdf",
        canonical_path,
        course_dir,
    )

    assert resolved == tracked_path
    assert tracked_path.read_bytes() == b"previous version"


@pytest.mark.asyncio
async def test_file_downloader(tmp_path):
    content = b"%PDF-1.7\nPDF payload data for testing downloader"
    url = "https://bildungsportal.sachsen.de/download/dummy.pdf"
    target = tmp_path / "dummy.pdf"

    async with respx.mock(base_url="https://bildungsportal.sachsen.de") as respx_mock:
        respx_mock.get("/download/dummy.pdf").respond(
            200,
            content=content,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/pdf",
                "ETag": '"etag999"',
            },
        )

        async with httpx.AsyncClient() as client:
            downloader = FileDownloader(client=client, request_delay_seconds=0.0)
            res = await downloader.download_file(url, target)

            assert res.success is True
            assert res.bytes_downloaded == len(content)
            assert res.etag == '"etag999"'
            assert target.is_file()
            assert target.read_bytes() == content


@pytest.mark.asyncio
async def test_file_downloader_preserves_explicit_versioned_target_name(tmp_path):
    content = b"%PDF-1.7\nupdated payload"
    original = tmp_path / "lecture.pdf"
    original.write_bytes(b"%PDF-1.4\noriginal payload")
    target = tmp_path / "lecture-1.pdf"
    url = "https://bildungsportal.sachsen.de/download/lecture.pdf"

    async with respx.mock(base_url="https://bildungsportal.sachsen.de") as respx_mock:
        respx_mock.get("/download/lecture.pdf").respond(
            200,
            content=content,
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="lecture.pdf"',
            },
        )

        async with httpx.AsyncClient() as client:
            downloader = FileDownloader(client=client, request_delay_seconds=0.0)
            result = await downloader.download_file(
                url,
                target,
                preserve_target_filename=True,
            )

    assert result.success is True
    assert target.read_bytes() == content
    assert original.read_bytes() == b"%PDF-1.4\noriginal payload"


@pytest.mark.asyncio
async def test_file_downloader_does_not_validate_pdf_contents(tmp_path):
    target = tmp_path / "bad.pdf"

    async with respx.mock(base_url="https://bildungsportal.sachsen.de") as respx_mock:
        respx_mock.get("/download/bad.pdf").respond(
            200,
            content=b"<!doctype html><html></html>",
            headers={"Content-Type": "application/pdf"},
        )

        async with httpx.AsyncClient() as client:
            downloader = FileDownloader(
                client=client,
                max_retries=1,
                request_delay_seconds=0.0,
            )
            result = await downloader.download_file(
                "https://bildungsportal.sachsen.de/download/bad.pdf",
                target,
            )

    assert result.success is True
    assert target.read_bytes() == b"<!doctype html><html></html>"
    assert not target.with_name("bad.pdf.part").exists()


@pytest.mark.asyncio
async def test_file_downloader_prefers_direct_pdf_url(tmp_path):
    target = tmp_path / "Beleg_1.pdf"
    post_url = "https://bildungsportal.sachsen.de/opal/auth/course?1-1.0-download"
    file_url = "https://bildungsportal.sachsen.de/opal/downloadering?fibercode=test-code"
    content = b"%PDF-1.7\nBeleg payload"
    request = DownloadRequest(
        url=post_url,
        data={
            "contentContainer:tableContainer:multiSelect:multiSelectGroup": "check13",
            "tableMultiActionContainer:tableMultiActionRepeater:1:tableMultiAction": "",
        },
        headers={
            "Wicket-Ajax": "true",
            "Wicket-FocusedElementId": "download-button",
            "Wicket-Ajax-BaseURL": "auth/RepositoryEntry/course",
        },
    )

    async with respx.mock(
        base_url="https://bildungsportal.sachsen.de",
        assert_all_called=False,
    ) as respx_mock:
        direct_route = respx_mock.get(
            "https://bildungsportal.sachsen.de/opal/auth/course/Beleg_1.pdf"
        ).respond(200, content=content, headers={"Content-Type": "application/pdf"})
        post_route = respx_mock.post(post_url).respond(500)

        async with httpx.AsyncClient() as client:
            downloader = FileDownloader(client=client, request_delay_seconds=0.0)
            result = await downloader.download_file(
                "https://bildungsportal.sachsen.de/opal/auth/course/Beleg_1.pdf",
                target,
                download_request=request,
            )

    assert result.success is True
    assert direct_route.called
    assert not post_route.called
    assert target.read_bytes() == content


@pytest.mark.asyncio
async def test_file_downloader_falls_back_to_opal_wicket_redirect(tmp_path):
    target = tmp_path / "Beleg_1.pdf"
    direct_url = "https://bildungsportal.sachsen.de/opal/auth/course/Beleg_1.pdf"
    post_url = "https://bildungsportal.sachsen.de/opal/auth/course?1-1.0-download"
    file_url = "https://bildungsportal.sachsen.de/opal/downloadering?fibercode=test-code"
    content = b"%PDF-1.7\nBeleg payload"
    request = DownloadRequest(
        url=post_url,
        data={
            "contentContainer:tableContainer:multiSelect:multiSelectGroup": "check13",
            "tableMultiActionContainer:tableMultiActionRepeater:1:tableMultiAction": "",
        },
        headers={
            "Wicket-Ajax": "true",
            "Wicket-FocusedElementId": "download-button",
            "Wicket-Ajax-BaseURL": "auth/RepositoryEntry/course",
        },
    )

    async with respx.mock(base_url="https://bildungsportal.sachsen.de") as respx_mock:
        direct_route = respx_mock.get(direct_url).respond(
            200,
            content=b"<html>not a PDF</html>",
            headers={"Content-Type": "text/html"},
        )
        post_route = respx_mock.post(post_url).respond(
            200,
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ajax-response><redirect><![CDATA['
                '/opal/downloadering?fibercode=test-code'
                ']]></redirect></ajax-response>'
            ),
            headers={"Content-Type": "text/xml"},
        )
        respx_mock.get(file_url).respond(
            200,
            content=content,
            headers={"Content-Type": "application/pdf"},
        )

        async with httpx.AsyncClient() as client:
            downloader = FileDownloader(client=client, request_delay_seconds=0.0)
            result = await downloader.download_file(
                direct_url,
                target,
                download_request=request,
            )

    assert result.success is True
    assert direct_route.called
    assert post_route.called
    assert b"check13" in post_route.calls[0].request.content
    assert post_route.calls[0].request.headers["Wicket-Ajax"] == "true"
    assert target.read_bytes() == content


@pytest.mark.asyncio
async def test_file_downloader_uses_opal_redirect_without_pdf_validation(tmp_path):
    target = tmp_path / "Beleg_1.pdf"
    post_url = "https://bildungsportal.sachsen.de/opal/auth/course?1-1.0-download"
    download_url = "https://bildungsportal.sachsen.de/opal/downloadering?fibercode=test-code"
    request = DownloadRequest(url=post_url, data={}, headers={})

    async with respx.mock(base_url="https://bildungsportal.sachsen.de") as respx_mock:
        respx_mock.get(
            "https://bildungsportal.sachsen.de/opal/auth/course/Beleg_1.pdf"
        ).respond(
            200,
            content=b"<html>not a PDF</html>",
            headers={"Content-Type": "text/html"},
        )
        respx_mock.post(post_url).respond(
            200,
            content=(
                '<ajax-response><redirect><![CDATA['
                '/opal/downloadering?fibercode=test-code'
                ']]></redirect></ajax-response>'
            ),
            headers={"Content-Type": "text/xml"},
        )
        respx_mock.get(download_url).respond(
            200,
            content=b"<html>not a PDF</html>",
            headers={"Content-Type": "text/html"},
        )

        async with httpx.AsyncClient() as client:
            downloader = FileDownloader(
                client=client,
                max_retries=1,
                request_delay_seconds=0.0,
            )
            result = await downloader.download_file(
                "https://bildungsportal.sachsen.de/opal/auth/course/Beleg_1.pdf",
                target,
                download_request=request,
            )

    assert result.success is True
    assert target.read_bytes() == b"<html>not a PDF</html>"


@pytest.mark.asyncio
async def test_sync_engine_dry_run(tmp_path, my_courses_html, course_folder_html):
    # Setup isolated test config and DB
    cfg = AppConfig()
    cfg.general.sync_directory = tmp_path / "OPAL_Sync"
    cfg.sync.request_delay_seconds = 0.0
    cfg.auth.username = "s12345"
    db = Database(tmp_path / "test.db")

    async with respx.mock(base_url="https://bildungsportal.sachsen.de") as respx_mock:
        # Session check route (valid)
        respx_mock.get("/opal/auth/resource/courses").respond(200, text=my_courses_html)
        # Course landing / tree routes
        respx_mock.get("/opal/auth/RepositoryEntry/12345678").respond(200, text=course_folder_html)
        respx_mock.get("/opal/auth/RepositoryEntry/87654321").respond(200, text=course_folder_html)
        respx_mock.get("/opal/auth/RepositoryEntry/99887766").respond(200, text=course_folder_html)

        # Node routes
        respx_mock.get(path__regex=r"/opal/auth/RepositoryEntry/\d+/CourseNode/\d+").respond(200, text=course_folder_html)

        async with httpx.AsyncClient() as client:
            engine = SyncEngine(config=cfg, db=db, client=client)
            # Preload cookies to bypass login
            client.cookies.set("JSESSIONID", "test_cookie", domain="bildungsportal.sachsen.de", path="/")

            summary = await engine.run_sync(dry_run=True)

            assert summary.status == "success"
            assert summary.courses_processed == 3
            assert summary.files_new > 0
            assert summary.files_failed == 0
            assert summary.duration_seconds >= 0
            # Under dry run, no files actually written to disk
            assert not (cfg.general.sync_directory / "WS 2026_27").exists()


@pytest.mark.asyncio
async def test_sync_reports_removed_files_without_deleting_local_copy(
    tmp_path, monkeypatch
):
    cfg = AppConfig()
    cfg.general.sync_directory = tmp_path / "OPAL_Sync"
    cfg.sync.request_delay_seconds = 0.0
    db = Database(tmp_path / "removed_file.db")
    course = db.upsert_course(
        Course(
            opal_id="123",
            name="Mathematik",
            url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/123",
        )
    )
    node = CourseNode(
        course_id=course.id,
        node_id="456",
        title="Kursmaterialien",
        url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/123/CourseNode/456",
    )
    local_path = (
        cfg.general.sync_directory
        / "Allgemein"
        / "Mathematik"
        / "Kursmaterialien"
        / "Materialien"
        / "alt.pdf"
    )
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"%PDF-1.7 old copy")
    db.upsert_file(
        FileEntry(
            course_id=course.id,
            remote_url="https://bildungsportal.sachsen.de/opal/files/alt.pdf",
            remote_path="Kursmaterialien/Materialien/alt.pdf",
            local_path=str(local_path),
            filename="alt.pdf",
            extension=".pdf",
            size_bytes=17,
        )
    )

    async def fetch_courses(_client):
        return [course]

    async def fetch_nodes(_client, _course_url, _course_id):
        return [node]

    async def fetch_files(
        client,
        node,
        allowed_extensions=None,
        include_extensions=None,
        exclude_extensions=None,
        include_excluded=False,
        known_folder_states=None,
        scanned_folder_states=None,
        scanned_folder_paths=None,
        observed_folder_paths=None,
        skipped_folder_paths=None,
        rescan=False,
        folder_callback=None,
    ):
        scanned_folder_paths.add("Kursmaterialien/Materialien")
        return []

    monkeypatch.setattr("opal_sync.sync.engine.fetch_my_courses", fetch_courses)
    monkeypatch.setattr("opal_sync.sync.engine.fetch_course_nodes", fetch_nodes)
    monkeypatch.setattr("opal_sync.sync.engine.fetch_folder_files", fetch_files)

    async with httpx.AsyncClient() as client:
        engine = SyncEngine(config=cfg, db=db, client=client)
        monkeypatch.setattr(engine, "ensure_authenticated", lambda _client: _async_true())
        summary = await engine.run_sync(course_filter="Mathematik")
        repeated_summary = await engine.run_sync(course_filter="Mathematik")

    assert summary.duration_seconds >= 0
    assert [(change.prefix, change.path) for change in summary.changes] == [
        ("-", local_path.relative_to(cfg.general.sync_directory).as_posix())
    ]
    assert local_path.is_file()
    stored = db.get_file(course.id, "https://bildungsportal.sachsen.de/opal/files/alt.pdf")
    assert stored is not None and stored.is_removed
    assert repeated_summary.changes == []


@pytest.mark.asyncio
async def test_sync_summary_counts_only_excluded_and_oversized_files(
    tmp_path, monkeypatch
):
    cfg = AppConfig()
    cfg.general.sync_directory = tmp_path / "OPAL_Sync"
    cfg.sync.request_delay_seconds = 0.0
    cfg.sync.exclude_extensions = [".mp4"]
    cfg.sync.max_file_size_mb = 1
    db = Database(tmp_path / "skip_counts.db")
    course = db.upsert_course(
        Course(
            opal_id="123",
            name="Mathematik",
            url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/123",
        )
    )
    node = CourseNode(
        course_id=course.id,
        node_id="456",
        title="Kursmaterialien",
        url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/123/CourseNode/456",
    )

    async def fetch_courses(_client):
        return [course]

    async def fetch_nodes(_client, _course_url, _course_id):
        return [node]

    async def fetch_files(client, node, include_excluded=False, **_kwargs):
        return [
            FileEntry(
                course_id=course.id,
                remote_url="https://example.com/video.mp4",
                remote_path="Kursmaterialien/video.mp4",
                local_path="",
                filename="video.mp4",
                extension=".mp4",
                size_bytes=3 * 1024 * 1024,
            ),
            FileEntry(
                course_id=course.id,
                remote_url="https://example.com/large.pdf",
                remote_path="Kursmaterialien/large.pdf",
                local_path="",
                filename="large.pdf",
                extension=".pdf",
                size_bytes=2 * 1024 * 1024,
            ),
        ]

    monkeypatch.setattr("opal_sync.sync.engine.fetch_my_courses", fetch_courses)
    monkeypatch.setattr("opal_sync.sync.engine.fetch_course_nodes", fetch_nodes)
    monkeypatch.setattr("opal_sync.sync.engine.fetch_folder_files", fetch_files)

    async with httpx.AsyncClient() as client:
        engine = SyncEngine(config=cfg, db=db, client=client)
        monkeypatch.setattr(engine, "ensure_authenticated", lambda _client: _async_true())
        summary = await engine.run_sync(course_filter="Mathematik")

    assert summary.files_skipped == 2
    assert summary.files_excluded == 1
    assert summary.files_too_large == 1
    assert summary.excluded_extensions == {".mp4": 1}
    assert summary.excluded_bytes == 2 * 1024 * 1024
    assert summary.files_new == 0
    assert summary.files_updated == 0


@pytest.mark.asyncio
async def test_sync_reports_friendly_error_when_connection_fails(tmp_path, monkeypatch):
    cfg = AppConfig()
    cfg.general.sync_directory = tmp_path / "OPAL_Sync"
    db = Database(tmp_path / "connection_error.db")

    async def fail_to_connect(_client):
        raise httpx.ConnectError("network is unreachable")

    async with httpx.AsyncClient() as client:
        engine = SyncEngine(config=cfg, db=db, client=client)
        monkeypatch.setattr(engine, "ensure_authenticated", fail_to_connect)
        summary = await engine.run_sync()

    assert summary.status == "error"
    assert summary.error_message == (
        "Unable to connect to OPAL. Check your internet connection and try again."
    )


async def _async_true():
    return True
