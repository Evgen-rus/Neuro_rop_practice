"""
Step 3. Run the local read-only deal customer-path pipeline.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from setup import BASE_DIR, get_logger


DEFAULT_DEAL_IDS = ["18507", "18493"]
DEFAULT_RAW_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEFAULT_MD_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "markdown"
DEFAULT_AUDIO_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "audio"

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run read-only Bitrix deal customer-path pipeline")
    parser.add_argument("--deal-ids", nargs="+", default=DEFAULT_DEAL_IDS, help="Deal IDs to process")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Raw JSON output dir")
    parser.add_argument("--markdown-dir", default=str(DEFAULT_MD_DIR), help="Markdown output dir")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="Call audio output dir")
    return parser.parse_args()


def run_step(command: list[str]) -> None:
    logger.info("Running: %s", " ".join(command))
    subprocess.run(command, cwd=BASE_DIR, check=True)


def main() -> None:
    args = parse_args()
    raw_dir = Path(args.raw_dir)
    markdown_dir = Path(args.markdown_dir)
    audio_dir = Path(args.audio_dir)

    run_step(
        [
            sys.executable,
            "bitrix/deals/fetch_deal_context.py",
            "--deal-ids",
            *args.deal_ids,
            "--output-dir",
            str(raw_dir),
        ]
    )
    run_step(
        [
            sys.executable,
            "bitrix/deals/download_call_audio.py",
            "--deal-ids",
            *args.deal_ids,
            "--raw-dir",
            str(raw_dir),
            "--audio-dir",
            str(audio_dir),
        ]
    )
    run_step(
        [
            sys.executable,
            "bitrix/deals/build_customer_path_report.py",
            "--input-dir",
            str(raw_dir),
            "--output-dir",
            str(markdown_dir),
            "--audio-dir",
            str(audio_dir),
            "--deal-ids",
            *args.deal_ids,
        ]
    )
    logger.info("Pipeline finished. Markdown reports: %s", markdown_dir)


if __name__ == "__main__":
    main()
