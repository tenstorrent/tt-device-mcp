# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The one logging shape for the package's long-running processes (spec 06 I10): the broker and
the health probe. Every line goes once to its own file in the log dir and once to stderr, through
one formatter. Kept out of server.py so a process that is not the broker can use it without
importing the broker."""

import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from tt_device_mcp.utils import open_readable

try:
    from zoneinfo import ZoneInfo

    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # tzdata missing — degrade to UTC-only rather than crash logging
    _PACIFIC = None


class _DualTZFormatter(logging.Formatter):
    """Stamp each record with both UTC and Pacific wall-clock so logs are readable
    by a UTC host and a Pacific reader without mental conversion, e.g.
    ``2026-06-11 15:25:04 UTC / 08:25:04 PDT``."""

    def formatTime(self, record, datefmt=None):  # noqa: ARG002 - datefmt unused by design
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        utc = dt.strftime("%Y-%m-%d %H:%M:%S")
        if _PACIFIC is None:
            return f"{utc} UTC"
        return f"{utc} UTC / {dt.astimezone(_PACIFIC).strftime('%H:%M:%S %Z')}"


def _log_uncaught_exception(exc_type, exc_value, exc_tb) -> None:
    """``sys.excepthook``: an exception that ends the process goes through the logger first, so
    the crash traceback is in the process's own log file and not only wherever stderr went (spec 06
    I10).

    ``KeyboardInterrupt`` is a stop, not a crash: uvicorn traps SIGINT (Ctrl-C on ``daemon
    start-fg``), shuts down cleanly and re-raises it, and that is the only way it reaches here.
    It is logged at INFO with its traceback kept: Ctrl-C on a hung broker is how an operator
    finds out where it was stuck. Every other type stays CRITICAL."""
    log = logging.getLogger("tt-device-mcp")
    if issubclass(exc_type, KeyboardInterrupt):
        log.info("stopped by SIGINT (Ctrl-C)", exc_info=(exc_type, exc_value, exc_tb))
        return
    log.critical("uncaught exception — the process is going down", exc_info=(exc_type, exc_value, exc_tb))


def _replace_handlers(target: logging.Logger, handlers: list[logging.Handler]) -> None:
    for old in list(target.handlers):
        target.removeHandler(old)
        old.close()
    for h in handlers:
        target.addHandler(h)


class _ReadableWatchedFileHandler(logging.handlers.WatchedFileHandler):
    """``WatchedFileHandler`` whose every open states the file's mode (spec 06 I9). The handler
    reopens its file itself after logrotate's rename, with a plain ``open`` that the process umask
    masks; the fresh file would then carry whatever the umask left, and the unit's ``UMask=0022``
    is a backstop, not the mechanism. So the mode is asserted here, at the one place every open of
    the path goes through — the first open at start included."""

    def _open(self):
        return open_readable(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors)


def setup_logging(log_file: Path, *, stderr_only_on_failure: bool = False) -> logging.Logger:
    """Every line once to ``log_file`` and once to stderr, through one formatter (spec 06 I10). The
    file handler is a ``WatchedFileHandler`` so logrotate's rename is followed by the next line
    landing in a fresh file at the path, 0644 by the handler's own chmod on every open
    (``_ReadableWatchedFileHandler``, spec 06 I9); the stderr copy is what the journal (system
    shape) or ``daemon.log`` (per-user shape) keeps. Installed on the ``tt-device-mcp`` logger,
    which does not propagate, and the same pair on the root logger.

    A file that cannot be opened raises, which fails the broker's start. With
    ``stderr_only_on_failure`` it is instead one warning and the stderr copy alone, the contract
    log.sh keeps for the deploy scripts: a process that can do its job without its log file does."""
    formatter = _DualTZFormatter(fmt="%(asctime)s | %(levelname)-5s | %(message)s")

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stderr_handler]
    failed = None
    try:
        file_handler = _ReadableWatchedFileHandler(log_file)
    except OSError as e:
        if not stderr_only_on_failure:
            raise
        failed = e
    else:
        file_handler.setFormatter(formatter)
        handlers.insert(0, file_handler)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    _replace_handlers(root, handlers)

    logger = logging.getLogger("tt-device-mcp")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    _replace_handlers(logger, handlers)

    sys.excepthook = _log_uncaught_exception
    if failed is not None:
        logger.warning(f"cannot write {log_file} ({failed}); this process logs to stderr only")
    return logger
