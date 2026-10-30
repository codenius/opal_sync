import re
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup

from opal_sync.utils.logging import get_logger

logger = get_logger()


class WicketResponse:
    """Helper to parse Wicket AJAX responses."""

    def __init__(self, raw_text: str):
        self.raw_text = raw_text
        self.components: Dict[str, str] = {}
        self.scripts: List[str] = []
        self.redirect_url: Optional[str] = None
        self.parse()

    def parse(self) -> None:
        if "<ajax-response>" not in self.raw_text:
            return

        try:
            root = ET.fromstring(self.raw_text)
            for comp in root.findall(".//component"):
                comp_id = comp.get("id", "")
                content = comp.text or ""
                if comp_id:
                    self.components[comp_id] = content

            self.scripts = [
                script.text or ""
                for script in root.findall(".//evaluate")
                if script.text
            ]

            redirect_elem = root.find(".//redirect")
            if redirect_elem is not None and redirect_elem.text:
                self.redirect_url = redirect_elem.text.strip()
        except ET.ParseError:
            # Fallback regex extraction if CDATA or XML is malformed
            comp_matches = re.findall(
                r'<component id="([^"]+)"[^>]*><!\[CDATA\[(.*?)\]\]></component>',
                self.raw_text,
                re.DOTALL
            )
            for comp_id, content in comp_matches:
                self.components[comp_id] = content

            redir_match = re.search(r'<redirect><!\[CDATA\[(.*?)\]\]></redirect>', self.raw_text)
            if redir_match:
                self.redirect_url = redir_match.group(1).strip()

    def get_combined_html(self) -> str:
        """Combine all component updates into one HTML string for soup parsing."""
        if not self.components:
            return self.raw_text
        return "\n".join(self.components.values())
