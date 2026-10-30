import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import httpx

from opal_sync.models.node import CourseNode
from opal_sync.utils.logging import get_logger

logger = get_logger()


def _parse_jstree_items(
    items: List[Dict[str, Any]],
    course_id: int,
    base_url: str,
    parent_path: str = "",
    has_structural_parent: bool = False,
) -> List[CourseNode]:
    nodes: List[CourseNode] = []
    for item in items:
        raw_id = str(item.get("id", ""))
        node_id = raw_id[2:] if raw_id.startswith("id") and raw_id[2:].isdigit() else raw_id

        a_attr = item.get("a_attr", {})
        title = a_attr.get("title")
        if not title:
            title = BeautifulSoup(item.get("text", ""), "html.parser").get_text(strip=True)

        title = re.sub(r"\s+", " ", title).strip() if title else f"Node {node_id}"

        href = a_attr.get("href")
        full_url = urljoin(base_url, href) if href else None

        icon_classes = set(str(item.get("icon", "")).lower().split())
        can_contain_files = bool({"icon-st", "icon-bc"} & icon_classes)
        node_type = "folder" if "icon-bc" in icon_classes else "structure"
        is_top_level_materials = (
            not has_structural_parent
            and node_type == "structure"
            and title.casefold() == "unterlagen"
        )
        relative_path = (
            parent_path
            if is_top_level_materials
            else f"{parent_path}/{title}".strip("/")
            if can_contain_files
            else parent_path
        )

        if node_id and can_contain_files:
            nodes.append(CourseNode(
                course_id=course_id,
                node_id=str(node_id),
                title=title,
                node_type=node_type,
                url=full_url,
                relative_path=relative_path,
            ))

        children = item.get("children", [])
        if isinstance(children, list) and children:
            nodes.extend(
                _parse_jstree_items(
                    children,
                    course_id,
                    base_url,
                    parent_path=relative_path,
                    has_structural_parent=(
                        has_structural_parent or can_contain_files
                    ),
                )
            )

    return nodes


def parse_course_tree_html(html: str, course_id: int, base_url: str) -> List[CourseNode]:
    """Parse the navigation menu of an OPAL course and identify nodes."""
    # Strategy 1: jsTree initial_data JSON embedded in scripts
    match = re.search(r'var initial_data\s*=\s*(\[.*?\]);', html, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            nodes = _parse_jstree_items(data, course_id, base_url)
            if nodes:
                logger.debug(f"Parsed {len(nodes)} course nodes from jsTree JSON")
                return nodes
        except Exception as e:
            logger.debug(f"Failed to parse jsTree JSON: {e}")

    # Strategy 2: Fallback HTML scraping
    soup = BeautifulSoup(html, "html.parser")
    nodes: List[CourseNode] = []
    seen_node_ids = set()

    node_links = soup.find_all("a", href=re.compile(r"CourseNode/([^/?#]+)"))
    for link in node_links:
        href = link.get("href", "")
        match = re.search(r"CourseNode/([^/?#]+)", href)
        if not match:
            continue

        node_id = match.group(1)
        if node_id in seen_node_ids:
            continue

        title = link.get_text(strip=True) or link.get("title", "").strip() or f"Node {node_id}"
        title = re.sub(r"\s+", " ", title).strip()

        node_type = "folder"
        link_classes = link.get("class", [])
        classes_str = " ".join(link_classes) if isinstance(link_classes, list) else str(link_classes)
        icon = link.find(["i", "span", "svg"])
        if icon:
            classes_str += " " + " ".join(icon.get("class", []))

        classes_lower = classes_str.lower()
        if "o_bc" in classes_lower or "folder" in classes_lower or "ordner" in classes_lower or "file" in classes_lower:
            node_type = "folder"
        elif "o_sp" in classes_lower or "singlepage" in classes_lower:
            node_type = "singlepage"
        elif "dialog" in classes_lower:
            node_type = "dialog"
        elif "task" in classes_lower or "o_ta" in classes_lower or "o_ita" in classes_lower:
            node_type = "task"

        full_url = urljoin(base_url, href)
        seen_node_ids.add(node_id)
        nodes.append(CourseNode(
            course_id=course_id,
            node_id=node_id,
            title=title,
            node_type=node_type,
            url=full_url,
        ))

    logger.debug(f"Found {len(nodes)} course nodes for course DB ID {course_id}")
    return nodes


async def fetch_course_nodes(client: httpx.AsyncClient, course_url: str, course_db_id: int) -> List[CourseNode]:
    """Fetch the course landing page and extract all navigable course elements/nodes."""
    resp = await client.get(course_url, follow_redirects=True)
    if resp.status_code != 200:
        logger.warning(f"Could not load course at {course_url} (HTTP {resp.status_code})")
        return []

    return parse_course_tree_html(resp.text, course_db_id, base_url=str(resp.url))
