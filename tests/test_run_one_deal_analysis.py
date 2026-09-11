from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_one_deal_analysis import (
    MODE_FULL,
    MODE_INCREMENTAL,
    assistant_command,
    assistant_env,
    incremental_block_reason,
    normalize_mode,
    parse_args,
    resolve_choice,
)
from storage.rop_db import init_db


class RunOneDealAnalysisTests(unittest.TestCase):
    def test_normalize_mode_accepts_numbers_and_names(self) -> None:
        self.assertEqual(normalize_mode("1"), MODE_FULL)
        self.assertEqual(normalize_mode("FULL"), MODE_FULL)
        self.assertEqual(normalize_mode("2"), MODE_INCREMENTAL)
        self.assertEqual(normalize_mode("incremental"), MODE_INCREMENTAL)
        self.assertIsNone(normalize_mode("mini"))

    def test_yes_requires_deal_and_mode(self) -> None:
        args = parse_args(["--yes", "--deal-id", "7"])
        with self.assertRaises(SystemExit):
            resolve_choice(args)

    def test_flags_skip_prompts(self) -> None:
        args = parse_args(["--yes", "--deal-id", "7567", "--mode", "incremental"])
        self.assertEqual(resolve_choice(args), ("7567", MODE_INCREMENTAL))

    def test_incremental_without_baseline_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite"
            init_db(db_path)
            reason = incremental_block_reason(db_path, "7")
        self.assertIsNotNone(reason)
        self.assertIn("нет доверенного", reason or "")

    def test_full_command_keeps_force_llm(self) -> None:
        command = assistant_command("7", MODE_FULL)
        self.assertTrue(any(item.endswith("run_rop_assistant.py") for item in command))
        self.assertIn("--yes", command)
        self.assertIn("7", command)
        self.assertNotIn("--no-force-llm", command)

    def test_incremental_command_disables_force_llm_and_sets_env(self) -> None:
        command = assistant_command("7", MODE_INCREMENTAL)
        self.assertIn("--no-force-llm", command)
        with patch.dict("os.environ", {"DEAL_INCREMENTAL_ANALYSIS_ENABLED": "false"}):
            self.assertEqual(assistant_env(MODE_FULL)["DEAL_INCREMENTAL_ANALYSIS_ENABLED"], "false")
            self.assertEqual(assistant_env(MODE_INCREMENTAL)["DEAL_INCREMENTAL_ANALYSIS_ENABLED"], "true")


if __name__ == "__main__":
    unittest.main()
