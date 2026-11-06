import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
from pathlib import PurePosixPath
import time
from typing import Callable, List, Optional
import httpx

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
from opal_sync.utils.sanitize import sanitize_filename

logger = get_logger()


@dataclass
class SyncItemReport:
    course_name: str
    node_title: str
    filename: str
    action: SyncAction
    details: str
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
        configured_path = next(
            (
                path
                for name, path in self.config.courses.paths.items()
                if name.casefold() == course.name.casefold()
            ),
            None,
        )
        if configured_path is None:
            return (
                self.config.general.sync_directory
                / sanitize_filename(course.name)
            )

        path = Path(configured_path).expanduser()
        if path.is_absolute():
            return path
        if ".." in path.parts:
            raise ValueError(
                f"Relative course path for '{course.name}' cannot contain '..'"
            )
        return self.config.general.sync_directory.joinpath(
            *(sanitize_filename(part) for part in path.parts if part not in ("", "."))
        )

    def _resolve_tracked_file_path(
        self,
        db_file: Optional[FileEntry],
        remote_path: Optional[str],
        base_file_path: Path,
        course_base_dir: Path,
    ) -> Path:
        if not db_file or db_file.remote_path != remote_path:
            return resolve_filename_conflict(
                target_path=base_file_path,
                remote_url=db_file.remote_url if db_file else "",
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
                logger.info(f"Processing course: [bold cyan]{db_course.name}[/bold cyan]")

                course_base_dir = self._course_base_dir(db_course)

                # 3. Discover course nodes (folders, pages)
                nodes = await fetch_course_nodes(client, db_course.url, db_course.id)
                known_folder_states = self.db.get_folder_states(db_course.id)
                previous_folder_states = dict(known_folder_states)
                previous_files = self.db.get_course_files(db_course.id)
                course_failures_before = summary.files_failed
                current_remote_urls = set()
                scanned_folder_paths: set[str] = set()
                observed_folder_paths: set[str] = set()
                skipped_folder_paths: set[str] = set()
                pending_folder_states: dict[str, tuple[str, str]] = {}
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
                        folder_callback=folder_callback,
                    )
                    scanned_folder_paths.update(node_scanned_paths)
                    observed_folder_paths.update(node_observed_paths)
                    skipped_folder_paths.update(node_skipped_paths)
                    # Keep these in memory until the whole course has finished.
                    # If traversal, a download, or later sync work aborts, the
                    # stored date remains old and the next run will revisit it.
                    pending_folder_states.update(scanned_folder_states)

                    for file_entry in files:
                        current_remote_urls.add(file_entry.remote_url)
                        if file_entry.remote_path:
                            parts = [sanitize_filename(p) for p in Path(file_entry.remote_path).parts]
                            base_file_path = course_base_dir.joinpath(*parts)
                        else:
                            base_file_path = node_dir / sanitize_filename(file_entry.filename)
                        db_file = self.db.get_file(db_course.id, file_entry.remote_url)
                        was_removed = bool(db_file and db_file.is_removed)

                        target_file_path = self._resolve_tracked_file_path(
                            db_file=db_file,
                            remote_path=file_entry.remote_path,
                            base_file_path=base_file_path,
                            course_base_dir=course_base_dir,
                        )
                        file_entry.local_path = str(target_file_path)

                        # Check diff
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
                            size_bytes=file_entry.size_bytes,
                        )

                        if action == SyncAction.SKIP_UNCHANGED:
                            if db_file and (
                                (
                                    file_entry.last_modified
                                    and file_entry.last_modified != db_file.last_modified
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
                                self.db.mark_file_present(db_course.id, file_entry.remote_url)
                            if was_removed and not dry_run:
                                summary.changes.append(
                                    SyncChange("+", self._relative_sync_path(target_file_path))
                                )
                            summary.items.append(item_report)
                            continue
                        elif action in (SyncAction.SKIP_EXCLUDED, SyncAction.SKIP_TOO_LARGE):
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
                                        summary.excluded_extensions.get(extension, 0) + 1
                                    )
                            else:
                                summary.files_too_large += 1
                                if file_entry.size_bytes:
                                    summary.excluded_bytes += file_entry.size_bytes
                            summary.files_skipped += 1
                            summary.items.append(item_report)
                            logger.debug(f"Skipping {file_entry.filename}: {reason}")
                            continue

                        if action == SyncAction.DOWNLOAD_UPDATE and target_file_path.is_file():
                            target_file_path = next_version_path(base_file_path)
                            file_entry.local_path = str(target_file_path)
                            item_report.filename = target_file_path.name

                        # File requires download
                        if dry_run:
                            if action == SyncAction.DOWNLOAD_NEW:
                                summary.files_new += 1
                            else:
                                summary.files_updated += 1
                            summary.items.append(item_report)
                            logger.info(f"[DRY-RUN] Would download: [yellow]{file_entry.filename}[/yellow] ({reason})")
                            continue

                        # Execute actual download with semaphore
                        async with semaphore:
                            last_modified = f" ({file_entry.last_modified})" if file_entry.last_modified else ""
                            logger.info(f"Downloading: [green]{target_file_path.name}[/green]{last_modified}")

                            def report_download_progress(
                                bytes_received: int,
                                total_bytes: Optional[int],
                            ) -> None:
                                if progress_callback:
                                    progress_callback(
                                        file_entry.filename,
                                        bytes_received,
                                        total_bytes or file_entry.size_bytes,
                                    )

                            active_download_report = item_report
                            if progress_callback:
                                progress_callback(
                                    file_entry.filename,
                                    0,
                                    file_entry.size_bytes,
                                )
                            dl_result = await self.downloader.download_file(
                                remote_url=file_entry.remote_url,
                                target_path=target_file_path,
                                progress_callback=report_download_progress,
                                download_request=file_entry.download_request,
                                preserve_target_filename=target_file_path != base_file_path,
                            )

                        if dl_result.success:
                            if action == SyncAction.DOWNLOAD_NEW:
                                summary.files_new += 1
                            else:
                                summary.files_updated += 1

                            summary.bytes_downloaded += dl_result.bytes_downloaded
                            item_report.size_bytes = dl_result.bytes_downloaded

                            # Update DB record
                            file_entry.local_path = str(target_file_path)
                            file_entry.size_bytes = dl_result.bytes_downloaded
                            file_entry.etag = dl_result.etag
                            file_entry.last_modified = (
                                file_entry.last_modified or dl_result.last_modified
                            )
                            file_entry.content_type = dl_result.content_type
                            if dl_result.resolved_filename:
                                file_entry.filename = dl_result.resolved_filename

                            self.db.upsert_file(file_entry)
                            summary.changes.append(
                                SyncChange(
                                    "+"
                                    if action == SyncAction.DOWNLOAD_NEW
                                    or db_file is None
                                    or was_removed
                                    else "~",
                                    self._relative_sync_path(target_file_path),
                                )
                            )
                            self.db.log_action(
                                run_id=run_id,
                                level="INFO",
                                action="DOWNLOAD",
                                course_name=db_course.name,
                                file_path=str(target_file_path),
                                details=f"Downloaded {dl_result.bytes_downloaded} B",
                            )
                        else:
                            summary.files_failed += 1
                            item_report.success = False
                            item_report.error = dl_result.error_message
                            logger.error(f"Failed to download {file_entry.filename}: {dl_result.error_message}")
                            self.db.log_action(
                                run_id=run_id,
                                level="ERROR",
                                action="DOWNLOAD_FAILED",
                                course_name=db_course.name,
                                file_path=str(target_file_path),
                                details=dl_result.error_message,
                            )

                        summary.items.append(item_report)
                        active_download_report = None

                if not dry_run:
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
                    if summary.files_failed == course_failures_before:
                        for folder_url, (remote_path, last_modified) in pending_folder_states.items():
                            self.db.upsert_folder_state(
                                course_id=db_course.id,
                                remote_url=folder_url,
                                remote_path=remote_path,
                                last_modified=last_modified,
                            )
                            known_folder_states[folder_url] = (remote_path, last_modified)
                    self.db.mark_course_synced(db_course.id)

        except asyncio.CancelledError:
            summary.status = "cancelled"
            summary.error_message = "Sync interrupted by Ctrl+C. Completed changes are listed below."
            if active_download_report is not None:
                active_download_report.success = False
                active_download_report.error = "Download interrupted by Ctrl+C."
                summary.files_failed += 1
                summary.items.append(active_download_report)
            logger.warning(summary.error_message)
        except KeyboardInterrupt:
            summary.status = "cancelled"
            summary.error_message = "Sync interrupted by Ctrl+C. Completed changes are listed below."
            if active_download_report is not None:
                active_download_report.success = False
                active_download_report.error = "Download interrupted by Ctrl+C."
                summary.files_failed += 1
                summary.items.append(active_download_report)
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
            if active_download_report is not None:
                active_download_report.success = False
                active_download_report.error = summary.error_message
                summary.files_failed += 1
                summary.items.append(active_download_report)
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
