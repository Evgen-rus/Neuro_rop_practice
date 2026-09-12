from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api import app as app_api
from api import llm_runtime


class LlmRuntimeStatusTests(unittest.TestCase):
    def test_missing_openrouter_key_reports_openai_fallback(self) -> None:
        with (
            patch.object(llm_runtime.llm_config, "LLM_PROVIDER", "openrouter"),
            patch.object(llm_runtime.llm_config, "LLM_FALLBACK_TO_OPENAI", True),
            patch.object(llm_runtime.llm_config, "OPENROUTER_API_KEY", ""),
            patch.object(llm_runtime.llm_config, "OPENAI_API_KEY", "configured"),
            patch.object(llm_runtime, "_latest_usage_event", return_value=None),
        ):
            result = llm_runtime.build_llm_runtime_status()
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["credential_configured"])
        self.assertIn("перейдут в OpenAI", result["status_label"])

    def test_last_event_exposes_only_safe_provider_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            path.write_text(
                "{broken}\n" + json.dumps({
                    "requested_at": "2026-09-13T10:00:00+00:00",
                    "provider": "openai",
                    "model": "gpt-safe",
                    "status": "success",
                    "error_type": "PermissionDeniedError",
                    "error_status_code": 403,
                    "error_code": "policy_violation",
                    "request_id": "req_safe123",
                    "provider_fallback": True,
                    "provider_fallback_reason": "AuthenticationError: status=401",
                    "validation_error": "private payload",
                }) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"OPENAI_USAGE_TRACE_PATH": str(path)}, clear=False):
                result = llm_runtime.build_llm_runtime_status()
        self.assertEqual(result["last_request"]["model"], "gpt-safe")
        self.assertTrue(result["last_request"]["provider_fallback"])
        self.assertEqual(result["last_request"]["error_status_code"], 403)
        self.assertIn("отклонил запрос", result["last_request"]["error_label"])
        self.assertEqual(result["last_request"]["request_id"], "req_safe123")
        self.assertNotIn("validation_error", result["last_request"])

    def test_unsafe_fallback_reason_is_not_exposed(self) -> None:
        with patch.object(llm_runtime, "_latest_usage_event", return_value={
            "provider": "openai",
            "provider_fallback": True,
            "provider_fallback_reason": "secret https://example.test/key",
        }):
            result = llm_runtime.build_llm_runtime_status()
        self.assertIsNone(result["last_request"]["provider_fallback_reason"])

    def test_endpoint_is_admin_only(self) -> None:
        with patch.object(app_api, "auth_current_user", return_value={"role": "manager"}):
            with self.assertRaises(HTTPException) as raised:
                app_api.llm_runtime_get()
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
