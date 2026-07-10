from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from openai_api.config import read_bool_env


class FeatureFlagTests(unittest.TestCase):
    def test_defaults_are_preserved_when_variables_are_absent(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(read_bool_env("CONTEXT_MEMORY_OPTIMIZATION_ENABLED", False))
            self.assertFalse(read_bool_env("CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE", False))
            self.assertTrue(read_bool_env("CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK", True))

    def test_explicit_values_are_parsed(self) -> None:
        with patch.dict(os.environ, {"FLAG": "true"}, clear=True):
            self.assertTrue(read_bool_env("FLAG", False))
        with patch.dict(os.environ, {"FLAG": "false"}, clear=True):
            self.assertFalse(read_bool_env("FLAG", True))


if __name__ == "__main__":
    unittest.main()
