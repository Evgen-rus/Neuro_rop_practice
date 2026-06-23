"""
Create per-lead/deal folders for the semi-manual ROP assistant workflow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.workspace import DEFAULT_DEAL_WORKSPACE_ROOT, ensure_deal_workspace
from setup import get_logger


logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare per-deal ROP assistant workspace folders")
    parser.add_argument("--deal-ids", nargs="+", required=True, help="Deal IDs to prepare")
    parser.add_argument("--workspace-root", default=str(DEFAULT_DEAL_WORKSPACE_ROOT), help="Deal workspace root")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.workspace_root)
    for deal_id in args.deal_ids:
        deal_dir = ensure_deal_workspace(str(deal_id), workspace_root=root)
        logger.info("Prepared deal workspace: %s", deal_dir)


if __name__ == "__main__":
    main()
