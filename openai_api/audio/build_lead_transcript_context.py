"""
Build one chronological transcript context file for a lead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.workspace import DEFAULT_LEAD_WORKSPACE_ROOT
from openai_api.audio.transcript_context import build_all_transcript_context


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build all-calls transcript context for a lead")
    parser.add_argument("--lead-id", required=True, help="Lead ID")
    parser.add_argument("--lead-root", default=str(DEFAULT_LEAD_WORKSPACE_ROOT), help="Root folder with lead workspaces")
    parser.add_argument("--output", default=None, help="Optional output .md path")
    return parser.parse_args()


def build_all_lead_transcript_context(lead_dir: Path, lead_id: str, output_path: Path | None = None) -> Path:
    return build_all_transcript_context(lead_dir, "lead", lead_id, output_path=output_path)


def main() -> None:
    args = parse_args()
    lead_dir = Path(args.lead_root) / f"lead_{args.lead_id}"
    output_path = Path(args.output) if args.output else None
    saved = build_all_lead_transcript_context(lead_dir, str(args.lead_id), output_path=output_path)
    print(f"All-calls transcript context saved: {saved}")


if __name__ == "__main__":
    main()
