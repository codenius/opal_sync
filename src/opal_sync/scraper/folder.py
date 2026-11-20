from pathlib import Path
import asyncio
from dataclasses import dataclass
import re
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
import httpx

from opal_sync.models.file_entry import FileEntry
from opal_sync.models.node import CourseNode
from opal_sync.scraper.file_links import (
    _find_opal_folder_form,
    _row_last_modified,
    extract_files_from_soup,
)
from opal_sync.scraper.wicket import WicketResponse
from opal_sync.utils.logging import get_logger

logger = get_logger()

FOLDER_REQUEST_RETRIES = 3
RETRYABLE_FOLDER_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class FolderLink:
    name: str
    url: str
    action_url: Optional[str]
    last_modified: Optional[str]


@dataclass
class FolderView:
    folder_url: str
    response_url: str
    soup: BeautifulSoup
    action_urls: Dict[str, str]
    wicket_base_url: Optional[str]
    used_ajax: bool


def _extract_wicket_actions(text: str, base_url: str) -> Dict[str, str]:
    actions = re.findall(
        r'Wicket\.Ajax\.ajax\(\{"u":"([^"]+)","c":"([^"]+)"',
        text,
    )
    return {component_id: urljoin(base_url, action_url) for action_url, component_id in actions}


def _extract_subfolders(
    soup: BeautifulSoup,
    base_url: str,
    action_urls: Dict[str, str],
) -> List[FolderLink]:
    """Find subfolder links in an OPAL folder table."""
    subfolders: List[FolderLink] = []
    folder_form = _find_opal_folder_form(soup)
    # Search table rows
    for row in soup.find_all("tr"):
        is_folder_row = False
        if folder_form:
            checkbox = row.find(
                "input",
                attrs={"type": "checkbox", "name": lambda name: name and "multiSelectGroup" in name},
            )
            if not checkbox or row.find_parent("form") is not folder_form:
                continue
            row_type = row.find(
                ["span", "i"],
                attrs={"title": lambda title: title and title.lower() in ("ordner", "folder")},
            ) or row.find(
                ["span", "i"],
                class_=lambda classes: classes and any(
                    "folder" in class_name.lower()
                    for class_name in (classes if isinstance(classes, list) else [classes])
                ),
            )
            if not row_type:
                continue
            is_folder_row = True
        elif not any(
            marker in row.get_text(" ", strip=True).lower()
            for marker in ("ordner", "folder")
        ):
            continue
        else:
            is_folder_row = True

        link = row.find("a", href=True)
        if not link:
            continue
        href = link.get("href", "")
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        name = link.get("data-file-name") or link.get_text(strip=True) or link.get("title", "").strip()
        if name and is_folder_row:
            subfolders.append(
                FolderLink(
                    name=name,
                    url=urljoin(base_url, href),
                    action_url=action_urls.get(link.get("id", "")),
                    last_modified=_row_last_modified(row),
                )
            )
    return subfolders


def _folder_link_base(folder_url: str) -> str:
    parts = urlsplit(folder_url)
    path = parts.path.rstrip("/") + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _normalized_folder_url(folder_url: str) -> tuple[str, str, str]:
    parts = urlsplit(folder_url)
    path = "/".join(unquote_plus(segment) for segment in parts.path.split("/"))
    return parts.scheme.casefold(), parts.netloc.casefold(), path.rstrip("/")


def _is_child_folder_url(folder_url: str, parent_url: str) -> bool:
    folder_scheme, folder_netloc, folder_path = _normalized_folder_url(folder_url)
    parent_scheme, parent_netloc, parent_path = _normalized_folder_url(parent_url)
    return (
        folder_scheme == parent_scheme
        and folder_netloc == parent_netloc
        and folder_path.startswith(parent_path + "/")
    )


def _same_folder_url(first_url: str, second_url: str) -> bool:
    return _normalized_folder_url(first_url) == _normalized_folder_url(second_url)


def _folder_url_alias_matches(first_url: str, second_url: str) -> bool:
    """Match OPAL folder aliases that omit or add an intermediate path segment."""
    def tail(url: str) -> tuple[str, ...]:
        parts = _normalized_folder_url(url)
        path = parts[2]
        marker = "/coursenode/"
        if marker not in path.casefold():
            return ()
        remainder = path.casefold().split(marker, 1)[1]
        _, separator, folder_path = remainder.partition("/")
        if not separator:
            return ()
        return tuple(folder_path.strip("/").split("/"))

    first_tail = tail(first_url)
    second_tail = tail(second_url)
    if not first_tail or not second_tail:
        return False
    return (
        len(first_tail) == len(second_tail) + 1
        and first_tail[-1] == second_tail[-1]
    ) or (
        len(second_tail) == len(first_tail) + 1
        and second_tail[-1] == first_tail[-1]
    )


def _find_folder_link(
    folders: List[FolderLink], expected_url: str
) -> Optional[FolderLink]:
    return next(
        (
            folder
            for folder in folders
            if _same_folder_url(folder.url, expected_url)
            or _folder_url_alias_matches(folder.url, expected_url)
        ),
        None,
    )


def _retry_delay(response: httpx.Response, attempt: int) -> Optional[float]:
    if (
        response.status_code not in RETRYABLE_FOLDER_STATUSES
        or attempt == FOLDER_REQUEST_RETRIES
    ):
        return None
    retry_after = response.headers.get("Retry-After")
    try:
        wait_time = float(retry_after) if retry_after else 1.5 ** attempt
    except ValueError:
        wait_time = 1.5 ** attempt
    return min(max(0.0, wait_time), 60.0)


def _repository_entry_id(url: str) -> Optional[str]:
    match = re.search(r"/RepositoryEntry/([^/?#]+)", url)
    return match.group(1) if match else None


async def _fetch_folder_view(
    client: httpx.AsyncClient,
    folder_url: str,
    action_url: Optional[str] = None,
    referer: Optional[str] = None,
    wicket_base_url: Optional[str] = None,
) -> Optional[FolderView]:
    headers = None
    if action_url:
        headers = {
            "Wicket-Ajax": "true",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer or folder_url,
        }
        if wicket_base_url:
            headers["Wicket-Ajax-BaseURL"] = wicket_base_url

    request_url = action_url or folder_url
    for attempt in range(1, FOLDER_REQUEST_RETRIES + 1):
        try:
            response = await client.get(
                request_url, follow_redirects=True, headers=headers
            )
            if response.status_code == 200:
                break
            wait_time = _retry_delay(response, attempt)
            if wait_time is None:
                logger.warning(
                    f"Could not fetch folder '{folder_url}' via {request_url} "
                    f"(HTTP {response.status_code}, attempt {attempt}/{FOLDER_REQUEST_RETRIES})"
                )
                return None
            logger.warning(
                f"HTTP {response.status_code} fetching folder '{folder_url}'. "
                f"Retrying in {wait_time:.1f}s (attempt {attempt}/{FOLDER_REQUEST_RETRIES})..."
            )
            await asyncio.sleep(wait_time)
        except httpx.TimeoutException as exc:
            if attempt == FOLDER_REQUEST_RETRIES:
                logger.error(
                    f"Timed out fetching folder '{folder_url}' after "
                    f"{FOLDER_REQUEST_RETRIES} attempts: {exc!r}"
                )
                raise
            wait_time = 1.5 ** attempt
            logger.warning(
                f"Timeout fetching folder '{folder_url}'. Retrying in "
                f"{wait_time:.1f}s (attempt {attempt}/{FOLDER_REQUEST_RETRIES})..."
            )
            await asyncio.sleep(wait_time)
    if response.status_code != 200:
        logger.warning(
            f"Could not fetch folder '{folder_url}' via {request_url} "
            f"(HTTP {response.status_code})"
        )
        return None

    response_url = str(response.url)
    if not action_url and not _same_folder_url(folder_url, response_url):
        logger.warning(
            f"Folder request redirected away from its listing: "
            f"{folder_url} -> {response_url}"
        )
        return None

    raw_text = response.text
    action_source = raw_text
    scripts: List[str] = []
    used_ajax = "<ajax-response>" in raw_text
    if used_ajax:
        wicket = WicketResponse(raw_text)
        if not wicket.components and wicket.redirect_url:
            redirect_target = response.headers.get("Ajax-Location") or wicket.redirect_url
            redirect_url = urljoin(response_url, redirect_target)
            expected_entry_id = _repository_entry_id(folder_url)
            redirect_entry_id = _repository_entry_id(redirect_url)
            if (
                expected_entry_id
                and redirect_entry_id
                and redirect_entry_id != expected_entry_id
            ):
                logger.warning(
                    f"Rejected Wicket redirect from course {expected_entry_id} "
                    f"to course {redirect_entry_id} while fetching '{folder_url}'"
                )
                return None
            logger.debug(
                f"Following OPAL Wicket redirect for folder '{folder_url}' "
                f"to {redirect_url}"
            )
            for attempt in range(1, FOLDER_REQUEST_RETRIES + 1):
                try:
                    response = await client.get(redirect_url, follow_redirects=True)
                except httpx.HTTPError as exc:
                    if attempt == FOLDER_REQUEST_RETRIES:
                        logger.warning(
                            f"Could not follow Wicket redirect for folder "
                            f"'{folder_url}' after {attempt} attempts: {exc}"
                        )
                        return None
                    wait_time = 1.5 ** attempt
                    logger.warning(
                        f"Could not follow Wicket redirect for folder '{folder_url}'. "
                        f"Retrying in {wait_time:.1f}s (attempt "
                        f"{attempt}/{FOLDER_REQUEST_RETRIES})..."
                    )
                    await asyncio.sleep(wait_time)
                    continue
                if response.status_code == 200:
                    break
                wait_time = _retry_delay(response, attempt)
                if wait_time is None:
                    logger.warning(
                        f"Wicket redirect for folder '{folder_url}' returned HTTP "
                        f"{response.status_code} after {attempt} attempts"
                    )
                    return None
                await asyncio.sleep(wait_time)
            response_url = str(response.url)
            raw_text = response.text
            action_source = raw_text
            used_ajax = "<ajax-response>" in raw_text
            if used_ajax:
                wicket = WicketResponse(raw_text)
        if used_ajax:
            if not wicket.components:
                logger.warning(
                    f"Folder navigation returned no listing for '{folder_url}' "
                    f"(requested {request_url})"
                )
                return None
            raw_text = wicket.get_combined_html()
            scripts = wicket.scripts

    soup = BeautifulSoup(raw_text, "html.parser")
    for script in scripts:
        script_tag = soup.new_tag("script")
        script_tag.string = script
        soup.append(script_tag)

    action_base = _folder_link_base(folder_url)
    action_urls = _extract_wicket_actions(action_source, action_base)
    if not wicket_base_url:
        match = re.search(
            r'Wicket\.Ajax\.baseUrl\s*=\s*"([^"]+)"',
            action_source,
        )
        if match:
            wicket_base_url = match.group(1)

    return FolderView(
        folder_url=folder_url,
        response_url=response_url,
        soup=soup,
        action_urls=action_urls,
        wicket_base_url=wicket_base_url,
        used_ajax=used_ajax,
    )


async def _crawl_folder(
    client: httpx.AsyncClient,
    root_node_url: str,
    folder_chain: List[FolderLink],
    course_id: int,
    node_id: Optional[int],
    rel_path: str,
    allowed_extensions: Optional[List[str]],
    include_extensions: Optional[List[str]],
    exclude_extensions: Optional[List[str]],
    include_excluded: bool,
    known_folder_states: Dict[str, tuple[str, str]],
    scanned_folder_states: Dict[str, tuple[str, str]],
    scanned_folder_paths: set[str],
    observed_folder_paths: set[str],
    skipped_folder_paths: set[str],
    rescan: bool = False,
    depth: int = 0,
    max_depth: int = 30,
    visited_folder_urls: Optional[set[tuple[str, str, str]]] = None,
    folder_callback: Optional[Callable[[str], None]] = None,
    task_downloads: bool = False,
    scan_errors: Optional[set[str]] = None,
    file_callback: Optional[Callable[[FileEntry], Awaitable[None]]] = None,
) -> List[FileEntry]:
    folder_url = folder_chain[-1].url if folder_chain else root_node_url
    if depth > max_depth:
        logger.warning(f"Maximum folder depth reached at {folder_url}")
        if scan_errors is not None:
            scan_errors.add(rel_path)
        return []

    visited = visited_folder_urls if visited_folder_urls is not None else set()
    folder_key = _normalized_folder_url(folder_url)
    if folder_key in visited:
        logger.debug(f"Skipping duplicate folder link: {folder_url}")
        return []

    logger.debug(f"Opening folder path: {folder_url}")
    if folder_callback:
        folder_callback(rel_path)
    view = await _fetch_folder_view(client, root_node_url)
    if view is None:
        if scan_errors is not None:
            scan_errors.add(rel_path)
        return []

    for step in folder_chain:
        parent_base = _folder_link_base(view.folder_url)
        available_folders = _extract_subfolders(
            view.soup,
            parent_base,
            view.action_urls,
        )
        current_step = _find_folder_link(available_folders, step.url)
        if current_step is None:
            for _ in range(FOLDER_REQUEST_RETRIES - 1):
                logger.debug(
                    f"Folder '{step.name}' was missing from listing "
                    f"{view.folder_url}; refreshing the parent listing"
                )
                view = await _fetch_folder_view(client, view.folder_url)
                if view is None:
                    if scan_errors is not None:
                        scan_errors.add(rel_path)
                    return []
                available_folders = _extract_subfolders(
                    view.soup,
                    _folder_link_base(view.folder_url),
                    view.action_urls,
                )
                current_step = _find_folder_link(available_folders, step.url)
                if current_step is not None:
                    break
        if current_step is None:
            logger.warning(
                f"Folder '{step.name}' was not present in listing {view.folder_url}; "
                f"preserving removal state for course {course_id}"
            )
            if scan_errors is not None:
                scan_errors.add(rel_path)
            return []
        if step.url in known_folder_states:
            observed_folder_paths.add(known_folder_states[step.url][0])
        if (
            not rescan
            and current_step.last_modified
            and step.url in known_folder_states
            and known_folder_states[step.url][1] == current_step.last_modified
        ):
            skipped_folder_paths.add(known_folder_states[step.url][0])
            logger.debug(
                f"Skipping unchanged folder '{step.name}' "
                f"(last modified {current_step.last_modified})"
            )
            visited.add(folder_key)
            return []
        if folder_callback:
            folder_callback(step.name)
        parent_view = view
        for action_attempt in range(FOLDER_REQUEST_RETRIES):
            view = await _fetch_folder_view(
                client,
                current_step.url,
                action_url=current_step.action_url,
                referer=parent_view.response_url,
                wicket_base_url=parent_view.wicket_base_url,
            )
            if view is None:
                if scan_errors is not None:
                    scan_errors.add(rel_path)
                return []
            if not current_step.action_url:
                break

            returned_folders = _extract_subfolders(
                view.soup,
                _folder_link_base(view.folder_url),
                view.action_urls,
            )
            returned_parent_listing = any(
                _same_folder_url(folder.url, current_step.url)
                for folder in returned_folders
            )
            has_out_of_parent_links = any(
                not _is_child_folder_url(
                    folder.url,
                    _folder_link_base(view.folder_url),
                )
                for folder in returned_folders
            )
            if not returned_parent_listing and not has_out_of_parent_links:
                break
            if action_attempt == FOLDER_REQUEST_RETRIES - 1:
                logger.warning(
                    f"Wicket action for folder '{current_step.url}' returned an "
                    "unrelated folder listing; retrying the direct folder URL"
                )
                direct_view = await _fetch_folder_view(client, current_step.url)
                if direct_view is None:
                    if scan_errors is not None:
                        scan_errors.add(rel_path)
                    return []
                view = direct_view
                break

            logger.debug(
                f"Wicket action for folder '{current_step.url}' returned an "
                "unrelated folder listing; refreshing the parent before retrying"
            )
            parent_view = await _fetch_folder_view(client, parent_view.folder_url)
            if parent_view is None:
                if scan_errors is not None:
                    scan_errors.add(rel_path)
                return []
            refreshed_folders = _extract_subfolders(
                parent_view.soup,
                _folder_link_base(parent_view.folder_url),
                parent_view.action_urls,
            )
            refreshed_step = _find_folder_link(refreshed_folders, step.url)
            if refreshed_step is None:
                logger.warning(
                    f"Folder '{step.name}' was not present in listing "
                    f"{parent_view.folder_url}; preserving removal state "
                    f"for course {course_id}"
                )
                if scan_errors is not None:
                    scan_errors.add(rel_path)
                return []
            current_step = refreshed_step

    current_base_url = _folder_link_base(view.folder_url)
    scanned_folder_paths.add(rel_path)
    subfolders = _extract_subfolders(view.soup, current_base_url, view.action_urls)
    if folder_chain and not view.used_ajax and any(
        _same_folder_url(subfolder.url, folder_url)
        for subfolder in subfolders
    ):
        logger.warning(f"Direct folder request returned its parent listing: {folder_url}")
        if scan_errors is not None:
            scan_errors.add(rel_path)
        return []
    visited.add(folder_key)

    files = extract_files_from_soup(
        soup=view.soup,
        base_url=current_base_url,
        course_id=course_id,
        node_id=node_id,
        allowed_extensions=allowed_extensions,
        include_extensions=include_extensions,
        exclude_extensions=exclude_extensions,
        include_excluded=include_excluded,
        task_downloads=task_downloads,
    )

    for f in files:
        f.remote_path = f"{rel_path}/{f.filename}" if rel_path else f.filename

    if file_callback:
        for file_entry in files:
            if file_entry.download_request:
                await file_callback(file_entry)

    current_folder_modified = (
        folder_chain[-1].last_modified if folder_chain else None
    )
    for subfolder in subfolders:
        if (
            view.used_ajax
            and not _is_child_folder_url(subfolder.url, current_base_url)
        ):
            logger.warning(
                f"Folder link is not beneath its parent: "
                f"{subfolder.url} (parent {current_base_url})"
            )
            if scan_errors is not None:
                scan_errors.add(rel_path)
            continue
        sub_rel_path = f"{rel_path}/{subfolder.name}" if rel_path else subfolder.name
        files.extend(
            await _crawl_folder(
                client=client,
                root_node_url=root_node_url,
                folder_chain=folder_chain + [subfolder],
                course_id=course_id,
                node_id=node_id,
                rel_path=sub_rel_path,
                allowed_extensions=allowed_extensions,
                include_extensions=include_extensions,
                exclude_extensions=exclude_extensions,
                include_excluded=include_excluded,
                known_folder_states=known_folder_states,
                scanned_folder_states=scanned_folder_states,
                scanned_folder_paths=scanned_folder_paths,
                observed_folder_paths=observed_folder_paths,
                skipped_folder_paths=skipped_folder_paths,
                rescan=rescan,
                depth=depth + 1,
                max_depth=max_depth,
                visited_folder_urls=visited,
                folder_callback=folder_callback,
                task_downloads=task_downloads,
                scan_errors=scan_errors,
                file_callback=file_callback,
            )
        )

    if current_folder_modified:
        scanned_folder_states[folder_url] = (rel_path, current_folder_modified)

    return files


async def fetch_folder_files(
    client: httpx.AsyncClient,
    node: CourseNode,
    allowed_extensions: Optional[List[str]] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None,
    include_excluded: bool = False,
    known_folder_states: Optional[Dict[str, tuple[str, str]]] = None,
    scanned_folder_states: Optional[Dict[str, tuple[str, str]]] = None,
    scanned_folder_paths: Optional[set[str]] = None,
    observed_folder_paths: Optional[set[str]] = None,
    skipped_folder_paths: Optional[set[str]] = None,
    rescan: bool = False,
    visited_folder_urls: Optional[set[tuple[str, str, str]]] = None,
    folder_callback: Optional[Callable[[str], None]] = None,
    scan_errors: Optional[set[str]] = None,
    file_callback: Optional[Callable[[FileEntry], Awaitable[None]]] = None,
) -> List[FileEntry]:
    """Fetch files inside a course node/folder, including subfolders."""
    if not node.url:
        if scan_errors is not None:
            scan_errors.add(node.relative_path or node.title)
        return []

    known_states = known_folder_states if known_folder_states is not None else {}
    scanned_states = scanned_folder_states if scanned_folder_states is not None else {}
    scanned_paths = scanned_folder_paths if scanned_folder_paths is not None else set()
    observed_paths = observed_folder_paths if observed_folder_paths is not None else set()
    skipped_paths = skipped_folder_paths if skipped_folder_paths is not None else set()
    files = await _crawl_folder(
        client=client,
        root_node_url=node.url,
        folder_chain=[],
        course_id=node.course_id,
        node_id=node.id,
        rel_path=(
            node.relative_path
            if node.relative_path is not None
            else node.title
        ),
        allowed_extensions=allowed_extensions,
        include_extensions=include_extensions,
        exclude_extensions=exclude_extensions,
        include_excluded=include_excluded,
        known_folder_states=known_states,
        scanned_folder_states=scanned_states,
        scanned_folder_paths=scanned_paths,
        observed_folder_paths=observed_paths,
        skipped_folder_paths=skipped_paths,
        rescan=rescan,
        visited_folder_urls=visited_folder_urls,
        folder_callback=folder_callback,
        task_downloads=node.node_type == "task",
        scan_errors=scan_errors,
        file_callback=file_callback,
    )

    logger.debug(
        f"Discovered total of {len(files)} files in node '{node.title}' "
        f"(including subfolders)"
    )
    return files
