from __future__ import annotations

import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from setup import MoscowTimedRotatingFileHandler


class LoggingSetupTests(unittest.TestCase):
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
