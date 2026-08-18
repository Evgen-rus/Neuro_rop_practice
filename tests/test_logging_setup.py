from __future__ import annotations

import io
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from setup import MoscowTimedRotatingFileHandler, configure_console


class LoggingSetupTests(unittest.TestCase):
    def test_configure_console_requests_utf8(self) -> None:
        stdout = Mock()
        stderr = Mock()
        with patch("setup.sys.stdout", stdout), patch("setup.sys.stderr", stderr):
            configure_console()
        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_configure_console_survives_streams_without_reconfigure(self) -> None:
        with patch("setup.sys.stdout", object()), patch("setup.sys.stderr", object()):
            configure_console()

    def test_console_handler_keeps_cyrillic_utf8_bytes(self) -> None:
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="utf-8", errors="replace")
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("leads_to_b24.test_utf8_console")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info("Выборка сделок deal-control ещё не настроена.")
        stream.flush()
        self.assertIn(
            "Выборка сделок deal-control ещё не настроена.".encode("utf-8"),
            buffer.getvalue(),
        )

    def test_rollover_continues_when_windows_keeps_log_file_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "shared.log"
            handler = MoscowTimedRotatingFileHandler(str(log_path))
            handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None))

            with patch.object(handler, "rotate", side_effect=PermissionError("file is in use")):
                handler.doRollover()

            self.assertIsNotNone(handler.stream)
            self.assertGreater(handler.rolloverAt, time.time())
            handler.close()


if __name__ == "__main__":
    unittest.main()
