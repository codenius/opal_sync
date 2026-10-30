import re
import unicodedata
from pathlib import Path


# Windows illegal filename characters: < > : " / \ | ? *
ILLEGAL_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}


def sanitize_filename(name: str, max_length: int = 240) -> str:
    """Sanitize a filename for safe usage on Windows and cross-platform filesystems."""
    if not name:
        return "unnamed"

    # Normalize unicode (NFC keeps German umlauts ä, ö, ü, ß nicely composed)
    name = unicodedata.normalize("NFC", name)

    # Strip whitespace and common trailing dots
    name = name.strip()

    # Replace illegal characters with underscore
    name = ILLEGAL_CHARS_PATTERN.sub("_", name)

    # Collapse multiple underscores/spaces
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"_+", "_", name)

    # Windows filenames cannot end with dot or space
    name = name.rstrip(". ")

    # Check reserved Windows device names
    stem = Path(name).stem.upper()
    if stem in RESERVED_NAMES:
        name = f"_{name}"

    if not name:
        name = "unnamed"

    # Enforce reasonable length constraint while keeping extension
    if len(name) > max_length:
        p = Path(name)
        ext = p.suffix
        stem_len = max_length - len(ext)
        if stem_len > 0:
            name = p.stem[:stem_len].rstrip(". ") + ext
        else:
            name = name[:max_length]

    return name


def sanitize_path_segment(segment: str) -> str:
    """Sanitize a folder/directory segment."""
    return sanitize_filename(segment)
