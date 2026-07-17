from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from bitrix.client import BitrixReadOnlyClient
from bitrix.deals.download_deals_call_audio import try_download_url
from reliability.retry import RetryPolicy


ZERO_DELAY_RETRY = RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0, jitter_ratio=0)


class FakeResponse:
    def __init__(self, status_code: int, *, payload: dict | None = None, chunks: list[bytes | BaseException] | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self._chunks = chunks or []
        self.headers = {"content-type": "audio/mpeg"}
        self.text = "failure"
        self.url = "https://example.test/call.mp3"
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, _chunk_size: int):
        for item in self._chunks:
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self):
        return None


class ExternalApiRetryTests(unittest.TestCase):
    def test_bitrix_retries_temporary_http_error(self) -> None:
        responses = [
            FakeResponse(503),
            FakeResponse(200, payload={"result": {"ID": "7"}}),
        ]
        with patch("bitrix.client.requests.post", side_effect=responses) as request:
            client = BitrixReadOnlyClient("https://example.test/hook", retry_policy=ZERO_DELAY_RETRY)
            result = client.call("crm.lead.get", {"id": "7"})
        self.assertEqual(result["result"]["ID"], "7")
        self.assertEqual(request.call_count, 2)

    def test_audio_retry_removes_partial_file_and_atomically_saves_result(self) -> None:
        responses = [
            FakeResponse(200, chunks=[b"partial", requests.ConnectionError("stream interrupted")]),
            FakeResponse(200, chunks=[b"complete-audio"]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with (
                patch("bitrix.deals.download_deals_call_audio.requests.get", side_effect=responses) as request,
                patch("bitrix.deals.download_deals_call_audio.DEFAULT_TRANSPORT_RETRY", ZERO_DELAY_RETRY),
                patch("bitrix.deals.download_deals_call_audio.enrich_download_with_duration", side_effect=lambda value: value),
            ):
                result = try_download_url("https://example.test/call.mp3", output, "call")
            self.assertTrue(result["ok"])
            self.assertEqual(Path(result["local_path"]).read_bytes(), b"complete-audio")
            self.assertEqual(list(output.glob("*.part")), [])
            self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
