"""
Run the local read-only deal customer-path pipeline.

Steps:
1. Fetch raw Bitrix deal context.
2. Build a readable Markdown customer-path report.
3. Download missing call audio files.
4. Prepare the per-deal ROP assistant workspace.
5. Build context diagnostics and compact LLM context.
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
from bitrix.customer_history import DEFAULT_HISTORY_DAYS


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
    parser.add_argument(
        "--skip-audio-download",
        action="store_true",
        help="Do not download missing call audio before workspace preparation.",
    )
    parser.add_argument(
        "--redownload-audio",
        action="store_true",
        help="Redownload call audio even if manifest already has existing local files.",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=DEFAULT_HISTORY_DAYS,
        help=f"Customer history period in days. Default: {DEFAULT_HISTORY_DAYS}",
    )
    parser.add_argument(
        "--include-related-contact-deals",
        action="store_true",
        help="Build full customer history through contact and related deals.",
    )
    parser.add_argument(
        "--include-internal-context",
        action="store_true",
        help="Include timeline comments/internal notes in full customer history.",
    )
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
            "--history-days",
            str(args.history_days),
            *(["--include-related-contact-deals"] if args.include_related_contact_deals else []),
            *(["--include-internal-context"] if args.include_internal_context else []),
        ]
    )
    if args.include_related_contact_deals:
        run_step(
            [
                sys.executable,
                "bitrix/customer_history_report.py",
                "--entity-type",
                "deal",
                "--input-dir",
                str(raw_dir),
                "--output-dir",
                str(markdown_dir),
                "--entity-ids",
                *args.deal_ids,
            ]
        )
    else:
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
    if not args.skip_audio_download:
        run_step(
            [
                sys.executable,
                "bitrix/deals/download_deals_call_audio.py",
                "--deal-ids",
                *args.deal_ids,
                "--raw-dir",
                str(raw_dir),
                "--audio-dir",
                str(audio_dir),
                *(["--redownload"] if args.redownload_audio else []),
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
    run_step(
        [
            sys.executable,
            "bitrix/context_diagnostics.py",
            "--entity-type",
            "deal",
            "--entity-ids",
            *args.deal_ids,
            "--workspace-root",
            str(workspace_root),
        ]
    )
    run_step(
        [
            sys.executable,
            "bitrix/deals/4_build_deals_llm_context.py",
            "--input-dir",
            str(raw_dir),
            "--workspace-root",
            str(workspace_root),
            "--deal-ids",
            *args.deal_ids,
        ]
    )
    logger.info("Deal pipeline finished. Workspace root: %s", workspace_root)


if __name__ == "__main__":
    main()
