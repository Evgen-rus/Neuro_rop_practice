"""Offline Phase 2 transport benchmark; never connects to Bitrix/OpenAI.

Unlike the live --full-cycle diagnostic, this uses synthetic source data and
acknowledges context-only runs without pretending that an analysis was performed.
"""
from __future__ import annotations
import argparse
import copy
import io
import json
import logging
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from crm_sync_fixture import SnapshotHarness


def run_fixture_benchmark(count: int = 10) -> dict:
    harness = SnapshotHarness(count)
    try:
        runs = []
        for name in ("A_initial_full", "B_immediate_idle", "C_one_new_activity"):
            if name.startswith("C"):
                deal_id = harness.ids[0]
                row = copy.deepcopy(harness.remote.activities[deal_id][0])
                row.update(ID="99999", LAST_UPDATED=harness.remote.now.isoformat(), SUBJECT="Новое синтетическое письмо")
                harness.remote.activities[deal_id].append(row)
            harness.remote.reset_counts()
            started = time.perf_counter()
            plans = harness.plans()
            heavy = 0
            for deal_id, plan in plans.items():
                if plan["mode"] in {"full", "incremental"}:
                    heavy += 1
                    harness.refresh(deal_id, plan)
            commands = dict(harness.remote.commands)
            runs.append({
                "scenario": name, "deals": count,
                "physical_transport_calls_simulated": harness.remote.physical,
                "actual_bitrix_http": 0,
                "logical_commands": sum(commands.values()),
                "heavy_context_fetch_count": heavy,
                "deals_skipped_before_heavy_fetch": sum(plan["mode"] == "skip" for plan in plans.values()),
                "audio_checks": 0, "llm_requests": 0,
                "timeline_requests": commands.get("crm.timeline.comment.list", 0),
                "chat_discovery_requests": commands.get("im.search.chat.list", 0),
                "invoice_product_requests": sum(commands.get(key, 0) for key in ("crm.invoice.list", "crm.item.list", "crm.deal.productrows.get")),
                "wall_seconds": round(time.perf_counter() - started, 3),
                "commands": commands,
            })
        return {
            "mode": "synthetic_transport_context_only",
            "limits": ["No network or paid calls", "Context-only acknowledgement, not a real LLM run",
                       "Deal-control/trajectory cost is not included", "No pending audio in this fixture; readiness is covered by unit tests",
                       "Do not compare simulated HTTP directly with the live OLD baseline"],
            "old_live_baseline_user_supplied": {"deals": 10, "bitrix_http": 396, "full": 0, "mini": 9, "skip": 1, "openai": 0},
            "runs": runs,
        }
    finally:
        harness.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    old_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()):
            result = run_fixture_benchmark(max(1, args.count))
    finally:
        logging.disable(old_disable)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
