import re
from typing import List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
import httpx

from opal_sync.models.course import Course
from opal_sync.utils.logging import get_logger

logger = get_logger()
OPAL_BASE_URL = "https://bildungsportal.sachsen.de"
MY_COURSES_URL = f"{OPAL_BASE_URL}/opal/auth/resource/courses"

SEMESTER_PATTERN = re.compile(
    r"(WS\s*\d{2,4}(?:/\d{2,4})?|SS\s*\d{2,4}|WiSe\s*\d{2,4}(?:/\d{2,4})?|SoSe\s*\d{2,4}|Sommersemester\s*\d{4}|Wintersemester\s*\d{4}(?:/\d{2,4})?|(?:(?<=\s)|(?<=,))\d{2}/\d{2}(?=\b))",
    re.IGNORECASE
)


def extract_semester(text: str) -> Optional[str]:
    """Extract semester string (e.g. 'WS 2026/27', 'SoSe 2026') from text."""
    match = SEMESTER_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    return None


def parse_my_courses_html(html: str, base_url: str = OPAL_BASE_URL) -> List[Course]:
    """Parse the 'Meine Kurse' HTML page and extract enrolled courses."""
    soup = BeautifulSoup(html, "html.parser")
    courses: List[Course] = []
    seen_ids = set()

    # OpenOLAT renders course items as links or table rows containing RepositoryEntry links
    # Pattern: /opal/auth/RepositoryEntry/123456789 or RepositoryEntry/123456789
    links = soup.find_all("a", href=re.compile(r"RepositoryEntry/\d+"))

    for link in links:
        href = link.get("href", "")
        href_parts = urlsplit(href)
        if href_parts.query:
            continue
        match = re.search(r"(?:^|/)RepositoryEntry/(\d+)/?$", href_parts.path)
        if not match:
            continue

        opal_id = match.group(1)
        if opal_id in seen_ids:
            continue

        # OPAL truncates visible course titles in tabs but keeps the full title attribute.
        title = link.get("title", "").strip() or link.get_text(strip=True)
        if title.lower().startswith(("http://", "https://")):
            title = ""

        if not title or title.endswith(("...", "…")):
            page_title = soup.title.get_text(strip=True) if soup.title else ""
            page_title = re.sub(r"\s+-\s+OPAL$", "", page_title)
            if page_title and page_title.lower() not in ("opal", "meine kurse"):
                title = page_title

        # Check surrounding table row / card if title is still empty or short
        if not title or len(title) < 2:
            parent_row = link.find_parent(["tr", "div", "li"])
            if parent_row:
                title_elem = parent_row.find(["h3", "h4", "h5", "strong", "b", "span", "a"])
                if title_elem:
                    parent_title = title_elem.get_text(strip=True)
                    if parent_title and not parent_title.lower().startswith(("http://", "https://")):
                        title = parent_title

        if not title:
            title = soup.title.get_text(strip=True) if soup.title else ""
            title = re.sub(r"\s+-\s+OPAL$", "", title)
        if not title:
            title = f"Course {opal_id}"

        # Clean title
        title = re.sub(r"\s+", " ", title).strip()

        full_url = urljoin(base_url, href)
        full_url_parts = urlsplit(full_url)
        full_url = urlunsplit(
            (full_url_parts.scheme, full_url_parts.netloc, full_url_parts.path, "", "")
        )
        semester = extract_semester(title)

        seen_ids.add(opal_id)
        courses.append(Course(
            opal_id=opal_id,
            name=title,
            url=full_url,
            semester=semester
        ))

    logger.debug(f"Discovered {len(courses)} courses from HTML")
    return courses


async def fetch_my_courses(client: httpx.AsyncClient) -> List[Course]:
    """Fetch the 'Meine Kurse' page and return list of enrolled courses."""
    resp = await client.get(MY_COURSES_URL, follow_redirects=True)
    if resp.status_code != 200:
        logger.error(f"Failed to fetch MyCourses (HTTP {resp.status_code})")
        return []

    return parse_my_courses_html(resp.text, base_url=str(resp.url))
