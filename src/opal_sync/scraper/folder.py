from pathlib import Path
import asyncio
from dataclasses import dataclass
import re
from typing import Callable, Dict, List, Optional
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
            break
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
) -> List[FileEntry]:
    folder_url = folder_chain[-1].url if folder_chain else root_node_url
    if depth > max_depth:
        logger.warning(f"Maximum folder depth reached at {folder_url}")
        return []

    visited = visited_folder_urls if visited_folder_urls is not None else set()
    folder_key = _normalized_folder_url(folder_url)
    if folder_key in visited:
        logger.debug(f"Skipping duplicate folder link: {folder_url}")
        return []
    visited.add(folder_key)

    logger.debug(f"Opening folder path: {folder_url}")
    if folder_callback:
        folder_callback(rel_path)
    view = await _fetch_folder_view(client, root_node_url)
    if view is None:
        return []

    for step in folder_chain:
        parent_base = _folder_link_base(view.folder_url)
        available_folders = _extract_subfolders(
            view.soup,
            parent_base,
            view.action_urls,
        )
        current_step = next(
            (folder for folder in available_folders if _same_folder_url(folder.url, step.url)),
            None,
        )
        if current_step is None:
            logger.warning(
                f"Folder '{step.name}' was not present in listing {view.folder_url}"
            )
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
            return []
        if folder_callback:
            folder_callback(step.name)
        view = await _fetch_folder_view(
            client,
            current_step.url,
            action_url=current_step.action_url,
            referer=view.response_url,
            wicket_base_url=view.wicket_base_url,
        )
        if view is None:
            return []

    current_base_url = _folder_link_base(view.folder_url)
    scanned_folder_paths.add(rel_path)
    subfolders = _extract_subfolders(view.soup, current_base_url, view.action_urls)
    if folder_chain and not view.used_ajax and any(
        _same_folder_url(subfolder.url, folder_url)
        for subfolder in subfolders
    ):
        logger.warning(f"Direct folder request returned its parent listing: {folder_url}")
        return []

    files = extract_files_from_soup(
        soup=view.soup,
        base_url=current_base_url,
        course_id=course_id,
        node_id=node_id,
        allowed_extensions=allowed_extensions,
        include_extensions=include_extensions,
        exclude_extensions=exclude_extensions,
        include_excluded=include_excluded,
    )

    for f in files:
        f.remote_path = f"{rel_path}/{f.filename}" if rel_path else f.filename

    current_folder_modified = (
        folder_chain[-1].last_modified if folder_chain else None
    )
    for subfolder in subfolders:
        if not _is_child_folder_url(subfolder.url, current_base_url):
            logger.warning(
                f"Folder link is not beneath its parent: "
                f"{subfolder.url} (parent {current_base_url})"
            )
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
    folder_callback: Optional[Callable[[str], None]] = None,
) -> List[FileEntry]:
    """Fetch files inside a course node/folder, including subfolders."""
    if not node.url:
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
        folder_callback=folder_callback,
    )

    logger.debug(
        f"Discovered total of {len(files)} files in node '{node.title}' "
        f"(including subfolders)"
    )
    return files
