import logging
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text


MAX_LOG_LINES = 2500


def _prune_log_file(path: Path) -> None:
    with path.open("rb") as log_file:
        lines = log_file.readlines()

    if len(lines) > MAX_LOG_LINES:
        with path.open("wb") as log_file:
            log_file.writelines(lines[len(lines) // 2 :])


class RichLinkHandler(RichHandler):
    """Rich logging handler that preserves pre-built Text styles and links."""

    def render_message(self, record: logging.LogRecord, message: str):
        rich_message = getattr(record, "rich_message", None)
        if isinstance(rich_message, Text):
            return rich_message
        return super().render_message(record, message)


console = Console()


def setup_logger(log_level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure structured console logging via rich and optional file logging."""
    logger = logging.getLogger("opal_sync")

    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Keep one console handler and refresh the file handler for the active
    # configuration. Setup can create a config after logging has already been
    # initialized, so a one-time initialization flag can silently omit the file.
    if not any(getattr(handler, "_opal_sync_console", False) for handler in logger.handlers):
        rich_handler = RichLinkHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True
        )
        rich_handler.setLevel(level)
        rich_handler._opal_sync_console = True
        logger.addHandler(rich_handler)

    for handler in logger.handlers:
        if getattr(handler, "_opal_sync_console", False):
            handler.setLevel(level)

    target_log = str(Path(log_file).expanduser().resolve()) if log_file else None
    existing_file_handlers = [
        handler for handler in logger.handlers
        if getattr(handler, "_opal_sync_file", False)
    ]
    if target_log and not any(
        getattr(handler, "_opal_sync_path", None) == target_log
        for handler in existing_file_handlers
    ):
        try:
            path = Path(target_log)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                _prune_log_file(path)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
            )
            file_handler.setFormatter(file_formatter)
            file_handler.setLevel(level)
            file_handler._opal_sync_file = True
            file_handler._opal_sync_path = target_log
            logger.addHandler(file_handler)
        except OSError as e:
            logger.warning(f"Could not initialize log file at {log_file}: {e}")

    for handler in existing_file_handlers:
        if target_log is None or getattr(handler, "_opal_sync_path", None) != target_log:
            logger.removeHandler(handler)
            handler.close()

    return logger


def get_logger() -> logging.Logger:
    """Return the shared opal_sync logger instance."""
    return logging.getLogger("opal_sync")
