"""
Run the local read-only lead customer-path pipeline.

Steps:
1. Fetch raw Bitrix lead context.
2. Build a readable Markdown customer-path report.
3. Download missing call audio files.
4. Prepare the per-lead ROP assistant workspace.
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


DEFAULT_RAW_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "raw"
DEFAULT_MD_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "markdown"
DEFAULT_AUDIO_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "audio"
DEFAULT_WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "leads"

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run read-only Bitrix lead customer-path pipeline")
    parser.add_argument("--lead-ids", nargs="+", required=True, help="Lead IDs to process")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Raw JSON output dir")
    parser.add_argument("--markdown-dir", default=str(DEFAULT_MD_DIR), help="Markdown output dir")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="Lead call audio manifest dir")
    parser.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT), help="Lead workspace root")
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
            "bitrix/leads/1_fetch_leads_context.py",
            "--lead-ids",
            *args.lead_ids,
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
                "lead",
                "--input-dir",
                str(raw_dir),
                "--output-dir",
                str(markdown_dir),
                "--entity-ids",
                *args.lead_ids,
            ]
        )
    else:
        run_step(
            [
                sys.executable,
                "bitrix/leads/2_build_leads_customer_path_report.py",
                "--input-dir",
                str(raw_dir),
                "--output-dir",
                str(markdown_dir),
                "--lead-ids",
                *args.lead_ids,
            ]
        )
    if not args.skip_audio_download:
        run_step(
            [
                sys.executable,
                "bitrix/leads/download_leads_call_audio.py",
                "--lead-ids",
                *args.lead_ids,
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
            "bitrix/leads/3_prepare_leads_workspace.py",
            "--lead-ids",
            *args.lead_ids,
            "--workspace-root",
            str(workspace_root),
        ]
    )
    run_step(
        [
            sys.executable,
            "bitrix/context_diagnostics.py",
            "--entity-type",
            "lead",
            "--entity-ids",
            *args.lead_ids,
            "--workspace-root",
            str(workspace_root),
        ]
    )
    logger.info("Lead pipeline finished. Workspace root: %s", workspace_root)


if __name__ == "__main__":
    main()
