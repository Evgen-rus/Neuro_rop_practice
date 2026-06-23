"""
Run the local read-only deal customer-path pipeline.

Steps:
1. Fetch raw Bitrix deal context.
2. Build a readable Markdown customer-path report.
3. Prepare the per-deal ROP assistant workspace.
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
DEFAULT_WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "deals"

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run read-only Bitrix deal customer-path pipeline")
    parser.add_argument("--deal-ids", nargs="+", default=DEFAULT_DEAL_IDS, help="Deal IDs to process")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Raw JSON output dir")
    parser.add_argument("--markdown-dir", default=str(DEFAULT_MD_DIR), help="Markdown output dir")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="Existing call audio manifest dir")
    parser.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT), help="Deal workspace root")
    return parser.parse_args()


def run_step(command: list[str]) -> None:
    logger.info("Running: %s", " ".join(command))
    subprocess.run(command, cwd=BASE_DIR, check=True)


def main() -> None:
    args = parse_args()
    raw_dir = Path(args.raw_dir)
    markdown_dir = Path(args.markdown_dir)
    audio_dir = Path(args.audio_dir)
    workspace_root = Path(args.workspace_root)

    run_step(
        [
            sys.executable,
            "bitrix/deals/1_fetch_deals_context.py",
            "--deal-ids",
            *args.deal_ids,
            "--output-dir",
            str(raw_dir),
        ]
    )
    run_step(
        [
            sys.executable,
            "bitrix/deals/2_build_deals_customer_path_report.py",
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
    run_step(
        [
            sys.executable,
            "bitrix/deals/3_prepare_deals_workspace.py",
            "--deal-ids",
            *args.deal_ids,
            "--workspace-root",
            str(workspace_root),
        ]
    )
    logger.info("Deal pipeline finished. Workspace root: %s", workspace_root)


if __name__ == "__main__":
    main()
