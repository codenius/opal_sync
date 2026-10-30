import logging
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.logging import RichHandler

console = Console()
_logger_initialized = False


def setup_logger(log_level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure structured console logging via rich and optional file logging."""
    global _logger_initialized
    logger = logging.getLogger("opal_sync")

    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Avoid adding duplicate handlers on multiple calls
    if not _logger_initialized:
        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True
        )
        rich_handler.setLevel(level)
        logger.addHandler(rich_handler)

        if log_file:
            try:
                path = Path(log_file).expanduser().resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(path, encoding="utf-8")
                file_formatter = logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
                )
                file_handler.setFormatter(file_formatter)
                file_handler.setLevel(level)
                logger.addHandler(file_handler)
            except OSError as e:
                logger.warning(f"Could not initialize log file at {log_file}: {e}")

        _logger_initialized = True

    return logger


def get_logger() -> logging.Logger:
    """Return the shared opal_sync logger instance."""
    return logging.getLogger("opal_sync")
