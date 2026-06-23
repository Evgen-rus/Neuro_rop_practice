"""
Create per-lead folders for the semi-manual ROP assistant workflow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.workspace import DEFAULT_LEAD_WORKSPACE_ROOT, ensure_entity_workspace
from setup import get_logger


logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare per-lead ROP assistant workspace folders")
    parser.add_argument("--lead-ids", nargs="+", required=True, help="Lead IDs to prepare")
    parser.add_argument("--workspace-root", default=str(DEFAULT_LEAD_WORKSPACE_ROOT), help="Lead workspace root")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.workspace_root)
    for lead_id in args.lead_ids:
        lead_dir = ensure_entity_workspace(str(lead_id), entity_type="lead", workspace_root=root)
        logger.info("Prepared lead workspace: %s", lead_dir)


if __name__ == "__main__":
    main()

