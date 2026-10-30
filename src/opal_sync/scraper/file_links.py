import re
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urljoin, urlsplit
from bs4 import BeautifulSoup
from bs4.element import Tag

from opal_sync.models.file_entry import DownloadRequest, FileEntry
from opal_sync.utils.logging import get_logger
from opal_sync.utils.extensions import get_file_extension, is_extension_allowed
from opal_sync.utils.sanitize import sanitize_filename

logger = get_logger()

# Common document and course file extensions
SIZE_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(B|KB|MB|GB|Bytes|kB)", re.I)


def parse_human_size(size_str: str) -> Optional[int]:
    """Parse strings like '1.5 MB' or '340 KB' into bytes."""
    match = SIZE_PATTERN.search(size_str)
    if not match:
        return None
    val = float(match.group(1).replace(",", "."))
    unit = match.group(2).upper()
    if unit in ("B", "BYTES"):
        return int(val)
    elif unit == "KB":
        return int(val * 1024)
    elif unit == "MB":
        return int(val * 1024 * 1024)
    elif unit == "GB":
        return int(val * 1024 * 1024 * 1024)
    return None


def _extract_download_request(
    soup: BeautifulSoup,
    row: Tag,
    base_url: str,
) -> Optional[DownloadRequest]:
    checkbox = row.find(
        "input",
        attrs={"type": "checkbox", "name": re.compile(r"multiSelectGroup")},
    )
    if not checkbox or not checkbox.get("name") or not checkbox.get("value"):
        return None

    form = checkbox.find_parent("form")
    if not form or not form.get("id"):
        return None

    download_button = None
    for button in form.find_all(["button", "input"]):
        name = button.get("name", "")
        label = " ".join(
            (button.get("title", ""), button.get_text(" ", strip=True))
        ).lower()
        if "tablemultiaction" in name.lower() and (
            "herunterladen" in label or "download" in label
        ):
            download_button = button
            break
    if not download_button or not download_button.get("id"):
        return None

    button_id = re.escape(download_button["id"])
    action_pattern = re.compile(
        r'Wicket\.Ajax\.ajax\(\{"u":"(?P<url>[^"]+)",'
        r'"m":"(?P<method>POST)","c":"' + button_id +
        r'","f":"(?P<form>[^"]+)","sc":"(?P<submit>[^"]+)"'
    )
    action = None
    for script in soup.find_all("script"):
        script_text = script.string or script.get_text()
        action = action_pattern.search(script_text)
        if action:
            break
    if not action or action.group("form") != form["id"]:
        return None

    form_data = {
        field["name"]: field.get("value", "")
        for field in form.find_all("input", attrs={"type": "hidden", "name": True})
    }
    form_data[checkbox["name"]] = checkbox["value"]
    form_data[action.group("submit")] = download_button.get("value", "")

    wicket_base = None
    base_pattern = re.compile(r'Wicket\.Ajax\.baseUrl\s*=\s*"([^"]+)"')
    for script in soup.find_all("script"):
        script_text = script.string or script.get_text()
        match = base_pattern.search(script_text)
        if match:
            wicket_base = match.group(1)
            break

    headers = {
        "Wicket-Ajax": "true",
        "Wicket-FocusedElementId": download_button["id"],
        "X-Requested-With": "XMLHttpRequest",
        "Referer": base_url,
    }
    if wicket_base:
        headers["Wicket-Ajax-BaseURL"] = wicket_base

    return DownloadRequest(
        url=urljoin(base_url, action.group("url")),
        data=form_data,
        headers=headers,
    )


def _find_opal_folder_form(soup: BeautifulSoup) -> Optional[Tag]:
    for form in soup.find_all("form"):
        has_file_selection = form.find(
            "input",
            attrs={"type": "checkbox", "name": re.compile(r"multiSelectGroup")},
        )
        has_download_action = form.find(
            ["button", "input"],
            attrs={"name": re.compile(r"tableMultiAction", re.I)},
        )
        if has_file_selection and has_download_action:
            return form
    return None


def _row_last_modified(row: Tag) -> Optional[str]:
    table = row.find_parent("table")
    if table:
        headers = table.select("thead th")
        for index, header in enumerate(headers):
            if header.get("data-header") == "table.folder.lastModified":
                cells = row.find_all("td", recursive=False)
                if index < len(cells):
                    value = cells[index].get_text(" ", strip=True)
                    return value or None

    date_values = [
        cell.get_text(" ", strip=True)
        for cell in row.find_all("td")
        if re.search(r"\d{2}\.\d{2}\.\d{4}", cell.get_text(" ", strip=True))
    ]
    return date_values[-1] if date_values else None


def extract_files_from_soup(
    soup: BeautifulSoup,
    base_url: str,
    course_id: int,
    node_id: Optional[int] = None,
    allowed_extensions: Optional[List[str]] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None,
    include_excluded: bool = False,
) -> List[FileEntry]:
    """Extract downloadable files from parsed HTML of an OPAL course folder/page."""
    files: List[FileEntry] = []
    include_filter = include_extensions
    if include_filter is None:
        include_filter = allowed_extensions or []
    opal_folder_form = _find_opal_folder_form(soup)

    # Strategy 1: Look at tables (standard OpenOLAT folder view)
    rows = soup.find_all("tr")
    for row in rows:
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
        if row_type:
            continue

        if opal_folder_form:
            row_checkbox = row.find(
                "input",
                attrs={"type": "checkbox", "name": re.compile(r"multiSelectGroup")},
            )
            if not row_checkbox or row.find_parent("form") is not opal_folder_form:
                continue

        link = row.find("a", href=True)
        if not link:
            continue

        href = link.get("href", "")
        # Skip pure javascript / anchor links unless they have download handlers
        if href.startswith("#") or href.startswith("javascript:void"):
            continue

        text = link.get_text(strip=True)
        filename = text or link.get("title", "").strip()

        # If filename doesn't have an extension, check URL path
        ext = get_file_extension(filename, include_filter + (exclude_extensions or []))
        if not ext:
            url_path = unquote(urlsplit(href).path)
            ext = get_file_extension(
                url_path, include_filter + (exclude_extensions or [])
            )
            if ext and not filename.endswith(ext):
                filename = f"{filename}{ext}"

        if not include_excluded and not is_extension_allowed(
            filename, include_filter, exclude_extensions
        ):
            continue

        if not filename or filename == href:
            filename = Path(unquote(urlsplit(href).path)).name

        if not filename:
            continue

        clean_name = sanitize_filename(filename)
        clean_ext = Path(clean_name).suffix.lower()

        full_url = urljoin(base_url, href)

        # Extract size and date from sibling cells if available
        size_bytes: Optional[int] = None
        date_str: Optional[str] = _row_last_modified(row)
        cells = row.find_all("td")
        for cell in cells:
            cell_text = cell.get_text(strip=True)
            if not size_bytes:
                size_bytes = parse_human_size(cell_text)

        files.append(FileEntry(
            course_id=course_id,
            node_id=node_id,
            remote_url=full_url,
            local_path="",  # will be assigned by sync engine
            filename=clean_name,
            extension=clean_ext,
            size_bytes=size_bytes,
            last_modified=date_str,
            download_request=_extract_download_request(soup, row, base_url),
        ))

    # OPAL's folder table has actionable file rows; other file-looking links on
    # the page (for example "recently changed" references) are not downloads.
    # Keep scanning anchors on pages without that folder-table structure.
    for link in soup.find_all("a", href=True) if opal_folder_form is None else []:
        if link.find_parent("tr"):
            continue
        if link.find_parent("footer"):
            continue
        href = link.get("href", "")
        if href.startswith("#") or href.startswith("javascript:"):
            continue

        full_url = urljoin(base_url, href)

        url_path = unquote(urlsplit(href).path)
        ext = get_file_extension(
            url_path, include_filter + (exclude_extensions or [])
        )

        if ext and (
            include_excluded
            or is_extension_allowed(url_path, include_filter, exclude_extensions)
        ):
            text_name = link.get_text(strip=True)
            filename = (
                text_name
                if get_file_extension(
                    text_name, include_filter + (exclude_extensions or [])
                )
                else Path(url_path).name
            )
            clean_name = sanitize_filename(filename)
            clean_ext = Path(clean_name).suffix.lower() or ext

            files.append(FileEntry(
                course_id=course_id,
                node_id=node_id,
                remote_url=full_url,
                local_path="",
                filename=clean_name,
                extension=clean_ext,
            ))

    return files
