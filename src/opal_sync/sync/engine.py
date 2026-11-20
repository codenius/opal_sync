import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
from pathlib import PurePosixPath
import time
from typing import Callable, List, Optional
import httpx
from rich.text import Text

from opal_sync.auth.credentials import get_credentials
from opal_sync.auth.session import SessionManager
from opal_sync.auth.shibboleth import login_shibboleth, ShibbolethAuthError
from opal_sync.config import AppConfig
from opal_sync.models.course import Course
from opal_sync.models.file_entry import FileEntry
from opal_sync.models.node import CourseNode
from opal_sync.scraper.course_tree import fetch_course_nodes
from opal_sync.scraper.courses import fetch_my_courses
from opal_sync.scraper.folder import fetch_folder_files
from opal_sync.storage.db import Database
from opal_sync.sync.conflict import next_version_path, resolve_filename_conflict
from opal_sync.sync.differ import SyncAction, determine_sync_action
from opal_sync.sync.downloader import FileDownloader
from opal_sync.utils.extensions import get_file_extension, is_extension_allowed
from opal_sync.utils.logging import get_logger
from opal_sync.utils.linking import course_directory, local_path_text
from opal_sync.utils.sanitize import sanitize_filename

logger = get_logger()


@dataclass
class SyncItemReport:
    course_name: str
    node_title: str
    filename: str
    action: SyncAction
    details: str
    local_path: Optional[Path] = None
    course_path: Optional[Path] = None
    size_bytes: Optional[int] = None
    success: bool = True
    error: Optional[str] = None


@dataclass
class SyncSummary:
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    status: str = "success"
    courses_processed: int = 0
    files_new: int = 0
    files_updated: int = 0
    files_skipped: int = 0
    files_excluded: int = 0
    files_too_large: int = 0
    excluded_extensions: dict[str, int] = field(default_factory=dict)
    excluded_bytes: int = 0
    files_failed: int = 0
    bytes_downloaded: int = 0
    items: List[SyncItemReport] = field(default_factory=list)
    changes: List["SyncChange"] = field(default_factory=list)
    duration_seconds: float = 0.0
    error_message: Optional[str] = None


@dataclass(frozen=True)
class SyncChange:
    prefix: str
    path: str


@dataclass
class DownloadJob:
    file_entry: FileEntry
    target_path: Path
    base_file_path: Path
    course_name: str
    course_id: int
    node_title: str
    action: SyncAction
    db_file: Optional[FileEntry]
    was_removed: bool
    item_report: "SyncItemReport"
    run_id: Optional[int]
    progress_callback: Optional[Callable[[str, int, Optional[int]], None]]
    completion: asyncio.Future


class SyncEngine:
    """Orchestrates discovering, diffing, downloading, and tracking course materials."""

    def __init__(
        self,
        config: AppConfig,
        db: Optional[Database] = None,
        session_mgr: Optional[SessionManager] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.config = config
        self.db = db or Database(
            config.db_path,
            sync_directory=config.general.sync_directory,
        )
        self.db.set_sync_directory(config.general.sync_directory)
        self.session_mgr = session_mgr or SessionManager(
            cookie_file=config.auth.session.cookie_file,
            session_ttl_hours=config.auth.session.session_ttl_hours,
        )
        self._external_client = client
        self.downloader: Optional[FileDownloader] = None
        self.last_summary: Optional[SyncSummary] = None

    def is_course_allowed(self, course_name: str) -> bool:
        """Check course name against include/exclude whitelist/blacklist rules."""
        name_lower = course_name.lower()

        if self.config.courses.include and not any(
            pattern.lower() in name_lower for pattern in self.config.courses.include
        ):
            return False

        if any(pattern.lower() in name_lower for pattern in self.config.courses.exclude):
            return False

        return True

    def _course_base_dir(self, course: Course) -> Path:
        return course_directory(
            self.config.general.sync_directory,
            self.config.courses.paths,
            course.name,
        )

    def _resolve_tracked_file_path(
        self,
        db_file: Optional[FileEntry],
        remote_path: Optional[str],
        base_file_path: Path,
        course_base_dir: Path,
    ) -> Path:
        if not db_file:
            return resolve_filename_conflict(
                target_path=base_file_path,
                remote_url="",
            )

        tracked_path = Path(db_file.local_path)
        try:
            tracked_path.resolve().relative_to(course_base_dir.resolve())
        except ValueError:
            if tracked_path.is_file() and not base_file_path.exists():
                base_file_path.parent.mkdir(parents=True, exist_ok=True)
                tracked_path.replace(base_file_path)
                logger.info(
                    f"Moved tracked file into course folder: "
                    f"{tracked_path.name} -> {base_file_path}"
                )
            return base_file_path

        if tracked_path.is_file():
            return tracked_path

        if db_file.remote_path != remote_path:
            return resolve_filename_conflict(
                target_path=base_file_path,
                remote_url=db_file.remote_url,
            )

        return tracked_path

    async def ensure_authenticated(self, client: httpx.AsyncClient) -> bool:
        """Verify active session or automatically re-authenticate with stored credentials."""
        # 0. Check if client already has an active valid session
        if await self.session_mgr.is_session_valid(client):
            logger.debug("Active client session is valid.")
            return True

        # 1. Try loading cached session cookies from disk
        has_session = self.session_mgr.load_session(client)
        if has_session:
            valid = await self.session_mgr.is_session_valid(client)
            if valid:
                logger.debug("Loaded OPAL session is valid.")
                return True
            logger.info("Saved OPAL session expired or invalid. Re-authenticating...")

        # 2. Get credentials: config password (dev) or keyring (production)
        username = self.config.auth.username
        if not username:
            raise ShibbolethAuthError("No username configured. Run 'opal-sync login' first.")

        password = self.config.auth.password or get_credentials(username)
        if not password:
            raise ShibbolethAuthError(
                f"No password found for user '{username}'. Set password in config or run 'opal-sync login'."
            )

        # 3. Authenticate via Shibboleth
        wayf_val = self.config.auth.wayf_value
        logger.info(f"Authenticating as {username} (university: {self.config.auth.university})...")
        success = await login_shibboleth(
            client=client,
            username=username,
            password=password,
            wayf_value=wayf_val,
        )

        if success:
            self.session_mgr.save_session(client)
            return True

        return False

    async def run_sync(
        self,
        dry_run: bool = False,
        course_filter: Optional[str] = None,
        force_download: bool = False,
        rescan: bool = False,
        progress_callback: Optional[Callable[[str, int, Optional[int]], None]] = None,
        folder_callback: Optional[Callable[[str], None]] = None,
    ) -> SyncSummary:
        """Execute full synchronization pass across all enrolled courses."""
        if self._external_client:
            return await self._execute_sync(self._external_client, dry_run, course_filter, force_download, rescan, progress_callback, folder_callback)

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            return await self._execute_sync(client, dry_run, course_filter, force_download, rescan, progress_callback, folder_callback)

    async def _execute_sync(
        self,
        client: httpx.AsyncClient,
        dry_run: bool = False,
        course_filter: Optional[str] = None,
        force_download: bool = False,
        rescan: bool = False,
        progress_callback: Optional[Callable[[str, int, Optional[int]], None]] = None,
        folder_callback: Optional[Callable[[str], None]] = None,
    ) -> SyncSummary:
        summary = SyncSummary()
        self.last_summary = summary
        active_download_report: Optional[SyncItemReport] = None
        active_download_reports: dict[int, SyncItemReport] = {}
        download_workers: list[asyncio.Task] = []
        sync_started = time.perf_counter()
        run_id = self.db.start_sync_run() if not dry_run else None

        try:
            # 1. Verify authentication
            await self.ensure_authenticated(client)

            # Initialize downloader
            max_bytes = (
                self.config.sync.max_file_size_mb * 1024 * 1024
                if self.config.sync.max_file_size_mb > 0
                else None
            )
            self.downloader = FileDownloader(
                client=client,
                request_delay_seconds=self.config.sync.request_delay_seconds,
                max_file_size_bytes=max_bytes,
            )

            # 2. Fetch all enrolled courses
            enrolled_courses = await fetch_my_courses(client)
            if not enrolled_courses:
                logger.warning("No courses discovered on OPAL dashboard.")

            # Concurrency semaphore for downloads
            semaphore = asyncio.Semaphore(self.config.sync.parallel_downloads)
            download_queue: asyncio.Queue = asyncio.Queue()
            download_worker_count = max(1, self.config.sync.parallel_downloads)
            active_download_reports: dict[int, SyncItemReport] = {}
            incomplete_courses: set[str] = set()
            pending_course_downloads: dict[int, set[asyncio.Future]] = {}
            pending_course_folder_states: dict[
                int, dict[str, tuple[str, str]]
            ] = {}

            async def process_download(job: DownloadJob) -> None:
                async with semaphore:
                    active_download_reports[id(job)] = job.item_report
                    if job.progress_callback:
                        job.progress_callback(
                            f"{job.course_name} - {job.file_entry.filename}",
                            0,
                            job.file_entry.size_bytes,
                        )

                    download_message = Text(
                        f"[{job.course_name}] Downloading: ",
                    )
                    download_message.append_text(
                        local_path_text(job.target_path, job.target_path.name)
                    )
                    if job.file_entry.last_modified:
                        download_message.append(
                            f" ({job.file_entry.last_modified})"
                        )
                    logger.info(download_message)

                    def report_download_progress(
                        bytes_received: int,
                        total_bytes: Optional[int],
                    ) -> None:
                        if job.progress_callback:
                            job.progress_callback(
                                f"{job.course_name} - {job.file_entry.filename}",
                                bytes_received,
                                total_bytes or job.file_entry.size_bytes,
                            )

                    dl_result = await self.downloader.download_file(
                        remote_url=job.file_entry.remote_url,
                        target_path=job.target_path,
                        progress_callback=report_download_progress,
                        download_request=job.file_entry.download_request,
                        preserve_target_filename=job.target_path != job.base_file_path,
                        course_name=job.course_name,
                    )

                active_download_reports.pop(id(job), None)
                if dl_result.success:
                    if job.action == SyncAction.DOWNLOAD_NEW:
                        summary.files_new += 1
                    else:
                        summary.files_updated += 1

                    summary.bytes_downloaded += dl_result.bytes_downloaded
                    job.item_report.size_bytes = dl_result.bytes_downloaded
                    job.item_report.local_path = job.target_path

                    job.file_entry.local_path = str(job.target_path)
                    job.file_entry.size_bytes = dl_result.bytes_downloaded
                    job.file_entry.etag = dl_result.etag
                    job.file_entry.last_modified = (
                        job.file_entry.last_modified or dl_result.last_modified
                    )
                    job.file_entry.content_type = dl_result.content_type
                    if dl_result.resolved_filename:
                        job.file_entry.filename = dl_result.resolved_filename

                    self.db.upsert_file(job.file_entry)
                    summary.changes.append(
                        SyncChange(
                            "+"
                            if job.action == SyncAction.DOWNLOAD_NEW
                            or job.db_file is None
                            or job.was_removed
                            else "~",
                            self._relative_sync_path(job.target_path),
                        )
                    )
                    self.db.log_action(
                        run_id=job.run_id,
                        level="INFO",
                        action="DOWNLOAD",
                        course_name=job.course_name,
                        file_path=str(job.target_path),
                        details=f"Downloaded {dl_result.bytes_downloaded} B",
                    )
                else:
                    summary.files_failed += 1
                    job.item_report.success = False
                    job.item_report.error = dl_result.error_message
                    logger.error(
                        f"[{job.course_name}] Failed to download "
                        f"{job.file_entry.filename}: {dl_result.error_message}"
                    )
                    self.db.log_action(
                        run_id=job.run_id,
                        level="ERROR",
                        action="DOWNLOAD_FAILED",
                        course_name=job.course_name,
                        file_path=str(job.target_path),
                        details=dl_result.error_message,
                    )

                summary.items.append(job.item_report)
                job.completion.set_result(dl_result.success)

            async def execute_download_job(job: DownloadJob) -> None:
                try:
                    await process_download(job)
                except asyncio.CancelledError:
                    active_download_reports.pop(id(job), None)
                    job.completion.cancel()
                    raise
                except Exception as exc:
                    logger.exception(
                        f"[{job.course_name}] Download worker failed for "
                        f"{job.file_entry.filename}: {exc}"
                    )
                    summary.files_failed += 1
                    job.item_report.success = False
                    job.item_report.error = str(exc)
                    summary.items.append(job.item_report)
                    job.completion.set_result(False)

            async def download_worker() -> None:
                while True:
                    job = await download_queue.get()
                    try:
                        if job is None:
                            return
                        await execute_download_job(job)
                    finally:
                        download_queue.task_done()

            download_workers = [
                asyncio.create_task(download_worker())
                for _ in range(download_worker_count)
            ]

            for course_data in enrolled_courses:
                # Optional filter by specific course name or ID from CLI argument
                if course_filter:
                    filter_lower = course_filter.lower()
                    if filter_lower not in course_data.name.lower() and filter_lower != course_data.opal_id:
                        continue

                # An explicit --course selection takes precedence over the
                # configured course filters. Those filters apply to ordinary
                # syncs across the enrolled course list.
                if not course_filter and not self.is_course_allowed(course_data.name):
                    course_data.is_excluded = True
                    self.db.upsert_course(course_data)
                    logger.debug(f"Skipping excluded course: [bold]{course_data.name}[/bold]")
                    continue

                # Upsert course in DB
                db_course = self.db.upsert_course(course_data)
                summary.courses_processed += 1
                course_base_dir = self._course_base_dir(db_course)
                course_heading = Text("Processing course: ")
                course_heading.append_text(
                    local_path_text(course_base_dir, db_course.name)
                )
                logger.info(course_heading)

                # 3. Discover course nodes (folders, pages)
                nodes = await fetch_course_nodes(client, db_course.url, db_course.id)
                known_folder_states = self.db.get_folder_states(db_course.id)
                previous_folder_states = dict(known_folder_states)
                previous_files = self.db.get_course_files(db_course.id)
                course_scan_errors: set[str] = set()
                current_remote_urls = set()
                scanned_folder_paths: set[str] = set()
                observed_folder_paths: set[str] = set()
                skipped_folder_paths: set[str] = set()
                pending_folder_states: dict[str, tuple[str, str]] = {}
                course_download_completions: set[asyncio.Future] = set()
                visited_folder_urls: set[tuple[str, str, str]] = set()
                visited_task_urls: set[tuple[str, str, str]] = set()
                for node_data in nodes:
                    db_node = self.db.upsert_node(node_data)
                    node_relative_path = (
                        node_data.relative_path
                        if node_data.relative_path is not None
                        else db_node.title
                    )
                    node_dir = course_base_dir.joinpath(
                        *(
                            sanitize_filename(part)
                            for part in Path(node_relative_path).parts
                            if part not in ("", ".")
                        )
                    )

                    # 4. Fetch files in node
                    scanned_folder_states = {}
                    node_scanned_paths: set[str] = set()
                    node_observed_paths: set[str] = set()
                    node_skipped_paths: set[str] = set()

                    async def process_file_entry(file_entry: FileEntry) -> None:
                        if file_entry.remote_url in current_remote_urls:
                            return
                        current_remote_urls.add(file_entry.remote_url)
                        if file_entry.remote_path:
                            parts = [
                                sanitize_filename(p)
                                for p in Path(file_entry.remote_path).parts
                            ]
                            base_file_path = course_base_dir.joinpath(*parts)
                        else:
                            base_file_path = (
                                node_dir / sanitize_filename(file_entry.filename)
                            )
                        db_file = self.db.get_file(
                            db_course.id, file_entry.remote_url
                        )
                        was_removed = bool(db_file and db_file.is_removed)

                        target_file_path = self._resolve_tracked_file_path(
                            db_file=db_file,
                            remote_path=file_entry.remote_path,
                            base_file_path=base_file_path,
                            course_base_dir=course_base_dir,
                        )
                        file_entry.local_path = str(target_file_path)

                        if force_download:
                            action = SyncAction.DOWNLOAD_UPDATE
                            reason = "Forced re-download requested"
                        else:
                            action, reason = determine_sync_action(
                                file_entry=file_entry,
                                target_local_path=target_file_path,
                                db_file=db_file,
                                include_extensions=self.config.sync.include_extensions,
                                exclude_extensions=self.config.sync.exclude_extensions,
                                max_file_size_bytes=max_bytes,
                            )

                        item_report = SyncItemReport(
                            course_name=db_course.name,
                            node_title=db_node.title,
                            filename=file_entry.filename,
                            action=action,
                            details=reason,
                            local_path=target_file_path,
                            course_path=course_base_dir,
                            size_bytes=file_entry.size_bytes,
                        )

                        if action == SyncAction.SKIP_UNCHANGED:
                            if db_file and (
                                (
                                    file_entry.last_modified
                                    and file_entry.last_modified
                                    != db_file.last_modified
                                )
                                or db_file.local_path != str(target_file_path)
                            ):
                                self.db.update_file_listing_metadata(
                                    course_id=db_course.id,
                                    remote_url=file_entry.remote_url,
                                    remote_path=file_entry.remote_path,
                                    size_bytes=None,
                                    last_modified=file_entry.last_modified,
                                    local_path=str(target_file_path),
                                )
                            if not dry_run:
                                self.db.mark_file_present(
                                    db_course.id, file_entry.remote_url
                                )
                            if was_removed and not dry_run:
                                summary.changes.append(
                                    SyncChange(
                                        "+",
                                        self._relative_sync_path(target_file_path),
                                    )
                                )
                            summary.items.append(item_report)
                            return
                        if action in (
                            SyncAction.SKIP_EXCLUDED,
                            SyncAction.SKIP_TOO_LARGE,
                        ):
                            if action == SyncAction.SKIP_EXCLUDED:
                                summary.files_excluded += 1
                                extension = get_file_extension(
                                    file_entry.filename,
                                    (
                                        self.config.sync.include_extensions
                                        + self.config.sync.exclude_extensions
                                    ),
                                )
                                if extension:
                                    summary.excluded_extensions[extension] = (
                                        summary.excluded_extensions.get(extension, 0)
                                        + 1
                                    )
                            else:
                                summary.files_too_large += 1
                                if file_entry.size_bytes:
                                    summary.excluded_bytes += file_entry.size_bytes
                            summary.files_skipped += 1
                            summary.items.append(item_report)
                            logger.debug(
                                f"Skipping {file_entry.filename}: {reason}"
                            )
                            return

                        if (
                            action == SyncAction.DOWNLOAD_UPDATE
                            and target_file_path.is_file()
                        ):
                            target_file_path = next_version_path(base_file_path)
                            file_entry.local_path = str(target_file_path)
                            item_report.filename = target_file_path.name
                            item_report.local_path = target_file_path

                        if dry_run:
                            if action == SyncAction.DOWNLOAD_NEW:
                                summary.files_new += 1
                            else:
                                summary.files_updated += 1
                            summary.items.append(item_report)
                            logger.info(
                                f"[{db_course.name}] [DRY-RUN] Would download: "
                                f"[yellow]{file_entry.filename}[/yellow] ({reason})"
                            )
                            return

                        completion = asyncio.get_running_loop().create_future()
                        course_download_completions.add(completion)
                        job = DownloadJob(
                            file_entry=file_entry,
                            target_path=target_file_path,
                            base_file_path=base_file_path,
                            course_name=db_course.name,
                            course_id=db_course.id,
                            node_title=db_node.title,
                            action=action,
                            db_file=db_file,
                            was_removed=was_removed,
                            item_report=item_report,
                            run_id=run_id,
                            progress_callback=progress_callback,
                            completion=completion,
                        )
                        if file_entry.download_request:
                            await execute_download_job(job)
                        else:
                            download_queue.put_nowait(job)

                    files = await fetch_folder_files(
                        client=client,
                        node=db_node,
                        include_extensions=self.config.sync.include_extensions,
                        exclude_extensions=self.config.sync.exclude_extensions,
                        include_excluded=True,
                        known_folder_states=known_folder_states,
                        scanned_folder_states=scanned_folder_states,
                        scanned_folder_paths=node_scanned_paths,
                        observed_folder_paths=node_observed_paths,
                        skipped_folder_paths=node_skipped_paths,
                        rescan=rescan,
                        visited_folder_urls=(
                            visited_task_urls
                            if db_node.node_type == "task"
                            else visited_folder_urls
                        ),
                        folder_callback=folder_callback,
                        scan_errors=course_scan_errors,
                        file_callback=process_file_entry,
                    )
                    scanned_folder_paths.update(node_scanned_paths)
                    observed_folder_paths.update(node_observed_paths)
                    skipped_folder_paths.update(node_skipped_paths)
                    # Save these only after the queued downloads finish. A
                    # failed or interrupted download must leave the old state
                    # so the next run will revisit the folder.
                    pending_folder_states.update(scanned_folder_states)

                    for file_entry in files:
                        if file_entry.remote_url not in current_remote_urls:
                            await process_file_entry(file_entry)

                pending_course_downloads[db_course.id] = (
                    course_download_completions
                )
                unaccounted_previous_files = []
                for previous_file in previous_files:
                    remote_parent = (
                        PurePosixPath(
                            previous_file.remote_path.replace("\\", "/")
                        ).parent.as_posix()
                        if previous_file.remote_path
                        else None
                    )
                    if remote_parent is None or not any(
                        remote_parent == path or remote_parent.startswith(path + "/")
                        for path in skipped_folder_paths
                    ):
                        unaccounted_previous_files.append(previous_file)
                if unaccounted_previous_files and not current_remote_urls:
                    course_scan_errors.add("<empty-course-scan>")
                    logger.warning(
                        f"Course '{db_course.name}' returned no files despite "
                        f"{len(unaccounted_previous_files)} previously tracked files "
                        "outside unchanged folders; "
                        "preserving removal state"
                    )

                if course_scan_errors:
                    incomplete_courses.add(db_course.name)
                    logger.warning(
                        f"Course '{db_course.name}' had incomplete folder scans at "
                        f"{', '.join(sorted(course_scan_errors))}; "
                        "skipping removal reconciliation and folder-state updates"
                    )

                if not dry_run and not course_scan_errors:
                    missing_folder_paths = set()
                    for _, (remote_path, _) in previous_folder_states.items():
                        folder_path = PurePosixPath(remote_path.replace("\\", "/"))
                        parent_path = folder_path.parent.as_posix()
                        if (
                            parent_path in scanned_folder_paths
                            and folder_path.as_posix() not in observed_folder_paths
                        ):
                            missing_folder_paths.add(folder_path.as_posix())

                    for previous_file in previous_files:
                        remote_path = previous_file.remote_path
                        if not remote_path or previous_file.remote_url in current_remote_urls:
                            continue
                        if not is_extension_allowed(
                            previous_file.filename,
                            self.config.sync.include_extensions,
                            self.config.sync.exclude_extensions,
                        ):
                            continue

                        remote_file_path = PurePosixPath(remote_path.replace("\\", "/"))
                        remote_parent = remote_file_path.parent.as_posix()
                        is_in_skipped_folder = any(
                            remote_parent == path or remote_parent.startswith(path + "/")
                            for path in skipped_folder_paths
                        )
                        if is_in_skipped_folder:
                            self.db.mark_file_present(
                                db_course.id, previous_file.remote_url
                            )
                            continue

                        is_in_removed_folder = any(
                            remote_parent == path or remote_parent.startswith(path + "/")
                            for path in missing_folder_paths
                        )
                        if (
                            remote_parent in scanned_folder_paths
                            or is_in_removed_folder
                        ) and self.db.mark_file_removed(
                            db_course.id, previous_file.remote_url
                        ):
                            summary.changes.append(
                                SyncChange(
                                    "-",
                                    self._relative_sync_path(
                                        Path(previous_file.local_path)
                                    ),
                                )
                            )

                # Mark course synced
                if not dry_run:
                    if not course_scan_errors:
                        pending_course_folder_states[db_course.id] = (
                            pending_folder_states
                        )
                    self.db.mark_course_synced(db_course.id)

            if incomplete_courses:
                summary.status = "partial"
                summary.error_message = (
                    "Incomplete folder scans prevented removal reconciliation for: "
                    + ", ".join(sorted(incomplete_courses))
                )

            for _ in download_workers:
                download_queue.put_nowait(None)
            await asyncio.gather(*download_workers)
            for course_id, folder_states in pending_course_folder_states.items():
                downloads_succeeded = all(
                    await asyncio.gather(
                        *pending_course_downloads.get(course_id, set())
                    )
                )
                if not downloads_succeeded:
                    continue
                for folder_url, (remote_path, last_modified) in folder_states.items():
                    self.db.upsert_folder_state(
                        course_id=course_id,
                        remote_url=folder_url,
                        remote_path=remote_path,
                        last_modified=last_modified,
                    )

        except asyncio.CancelledError:
            summary.status = "cancelled"
            summary.error_message = "Sync interrupted by Ctrl+C. Completed changes are listed below."
            for report in active_download_reports.values():
                report.success = False
                report.error = "Download interrupted by Ctrl+C."
                summary.files_failed += 1
                summary.items.append(report)
            for worker in download_workers:
                worker.cancel()
            await asyncio.gather(*download_workers, return_exceptions=True)
            logger.warning(summary.error_message)
        except KeyboardInterrupt:
            summary.status = "cancelled"
            summary.error_message = "Sync interrupted by Ctrl+C. Completed changes are listed below."
            for report in active_download_reports.values():
                report.success = False
                report.error = "Download interrupted by Ctrl+C."
                summary.files_failed += 1
                summary.items.append(report)
            for worker in download_workers:
                worker.cancel()
            await asyncio.gather(*download_workers, return_exceptions=True)
            logger.warning(summary.error_message)
        except Exception as e:
            logger.exception(f"Sync failed with unhandled exception: {e}")
            summary.status = "error"
            if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                summary.error_message = (
                    "Unable to connect to OPAL. Check your internet connection "
                    "and try again."
                )
            elif isinstance(e, httpx.ReadTimeout):
                summary.error_message = (
                    "Timed out waiting for data from OPAL. Your network may be "
                    "slow or temporarily stalled. Check your connection and try again."
                )
            else:
                summary.error_message = str(e)
            for report in active_download_reports.values():
                report.success = False
                report.error = summary.error_message
                summary.files_failed += 1
                summary.items.append(report)
            for worker in download_workers:
                worker.cancel()
            await asyncio.gather(*download_workers, return_exceptions=True)
            if run_id:
                self.db.log_action(
                    run_id=run_id,
                    level="ERROR",
                    action="RUN_ERROR",
                    details=str(e),
                )
        finally:
            summary.finished_at = datetime.utcnow()
            summary.duration_seconds = time.perf_counter() - sync_started
            if run_id:
                self.db.finish_sync_run(
                    run_id=run_id,
                    status=summary.status,
                    files_new=summary.files_new,
                    files_updated=summary.files_updated,
                    files_skipped=summary.files_skipped,
                    files_failed=summary.files_failed,
                    bytes_downloaded=summary.bytes_downloaded,
                    error_message=summary.error_message,
                )

        return summary

    def _relative_sync_path(self, path: Path) -> str:
        return Path(
            os.path.relpath(
                Path(path).resolve(),
                self.config.general.sync_directory.resolve(),
            )
        ).as_posix()
