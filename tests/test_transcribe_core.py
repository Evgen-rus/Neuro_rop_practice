from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import soundfile as sf

from openai_api.audio.transcribe_core import transcribe_file_async


class TranscribeCoreLongFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_chunks_and_covers_exact_duration_with_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp3"
            source.write_bytes(b"source")

            def convert(command, **_kwargs):
                sf.write(command[-1], np.arange(40, dtype=np.int16), 10, subtype="PCM_16")

            transcribe = AsyncMock(side_effect=["один", "два", "три"])
            with patch("openai_api.audio.transcribe_core.SAFE_CHUNK_SECONDS", 2), \
                 patch("openai_api.audio.transcribe_core.subprocess.run", side_effect=convert), \
                 patch("openai_api.audio.transcribe_core.sf.read", side_effect=AssertionError("full read")), \
                 patch("openai_api.audio.transcribe_core.transcribe_voice", transcribe), \
                 patch("openai_api.audio.transcribe_core._record_transcription_spend") as spend:
                result = await transcribe_file_async(str(source), chunk_overlap_seconds=1)

        self.assertEqual(transcribe.await_count, 3)
        self.assertIn("(0.0–2.0 сек)", result)
        self.assertIn("(1.0–3.0 сек)", result)
        self.assertIn("(2.0–4.0 сек)", result)
        self.assertEqual(spend.call_count, 3)

    async def test_conversion_failure_removes_temp_wav(self) -> None:
        created: list[Path] = []

        def fail(command, **_kwargs):
            created.append(Path(command[-1]))
            raise subprocess.CalledProcessError(1, command)

        with tempfile.NamedTemporaryFile(suffix=".mp3") as source, \
             patch("openai_api.audio.transcribe_core.subprocess.run", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg"):
                await transcribe_file_async(source.name)

        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())


if __name__ == "__main__":
    unittest.main()
