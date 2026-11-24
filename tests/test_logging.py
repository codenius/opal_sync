import logging
from contextlib import contextmanager

from opal_sync.utils.logging import setup_logger


@contextmanager
def configured_logger(log_path):
    logger = logging.getLogger("opal_sync")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    logger.handlers = []

    try:
        yield setup_logger("INFO", str(log_path))
    finally:
        for handler in logger.handlers:
            logger.removeHandler(handler)
            handler.close()
        logger.handlers = original_handlers
        logger.setLevel(original_level)


def test_logger_setup_keeps_newest_half_when_file_exceeds_limit(tmp_path):
    log_path = tmp_path / "opal-sync.log"
    original_lines = [f"entry {index}\n" for index in range(3000)]
    log_path.write_text("".join(original_lines), encoding="utf-8")

    with configured_logger(log_path):
        lines = log_path.read_text(encoding="utf-8").splitlines()

    assert lines == [f"entry {index}" for index in range(1500, 3000)]


def test_logger_setup_trims_once_per_invocation(tmp_path):
    log_path = tmp_path / "opal-sync.log"
    original_lines = [f"entry {index}\n" for index in range(2500)]
    log_path.write_text("".join(original_lines), encoding="utf-8")

    with configured_logger(log_path) as logger:
        logger.info("new entry")
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2501

    with configured_logger(log_path):
        lines = log_path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 1251
    assert lines[0] == "entry 1250"
    assert lines[-1].endswith("new entry")
