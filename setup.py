"""
Logging setup shared across project scripts.
"""

import logging
import re
import time
from datetime import datetime, time as dt_time, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

MSK_TZ = ZoneInfo("Europe/Moscow")
LOG_LEVEL = logging.INFO
LOG_RETENTION_DAYS = 14


class MoscowFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=MSK_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


class MoscowTimedRotatingFileHandler(TimedRotatingFileHandler):
    def __init__(self, filename: str):
        super().__init__(
            filename=filename,
            when="midnight",
            interval=1,
            backupCount=LOG_RETENTION_DAYS,
            encoding="utf-8",
            utc=False,
        )

    def computeRollover(self, current_time: float) -> float:
        current_dt = datetime.fromtimestamp(current_time, tz=MSK_TZ)
        next_midnight = datetime.combine(
            current_dt.date() + timedelta(days=1),
            dt_time.min,
            tzinfo=MSK_TZ,
        )
        return next_midnight.timestamp()

    def shouldRollover(self, record) -> int:
        return 1 if record.created >= self.rolloverAt else 0

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        rollover_at = self.rolloverAt
        suffix_time = datetime.fromtimestamp(rollover_at - 1, tz=MSK_TZ).timetuple()
        dfn = self.rotation_filename(
            self.baseFilename + "." + time.strftime(self.suffix, suffix_time)
        )

        if Path(self.baseFilename).exists():
            if Path(dfn).exists():
                Path(dfn).unlink()
            self.rotate(self.baseFilename, dfn)

        if self.backupCount > 0:
            for old_log in self.getFilesToDelete():
                Path(old_log).unlink(missing_ok=True)

        if not self.delay:
            self.stream = self._open()

        current_time = time.time()
        new_rollover_at = self.computeRollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at += self.interval
        self.rolloverAt = new_rollover_at


def _normalize_script_name(script_name: str) -> str:
    stem = Path(script_name).stem or "app"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem)


def get_logger(script_name: str) -> logging.Logger:
    normalized_name = _normalize_script_name(script_name)
    logger = logging.getLogger(f"leads_to_b24.{normalized_name}")

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    formatter = MoscowFormatter(
        "%(asctime)s MSK - %(name)s - %(levelname)s - %(message)s"
    )
    log_file = LOGS_DIR / f"{normalized_name}.log"

    file_handler = MoscowTimedRotatingFileHandler(str(log_file))
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
