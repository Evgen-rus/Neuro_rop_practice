"""Purge manifest-managed Bitrix audio only after its transcript bundle exists."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import load_json
from bitrix.deals.download_deals_call_audio import record_transcribed_and_purged


MANIFEST_ROOTS = (
    PROJECT_ROOT / "reports" / "bitrix_customer_path" / "audio",
    PROJECT_ROOT / "reports" / "bitrix_lead_path" / "audio",
)


@dataclass(frozen=True)
class PurgeCandidate:
    manifest_path: Path
    activity_id: str
    audio_path: Path
    transcript_json_path: Path


def entity_from_manifest(manifest_path: Path) -> tuple[str, str]:
    entity_type = "deal" if manifest_path.name.startswith("deal_") else "lead"
    entity_id = manifest_path.stem.split("_")[1]
    return entity_type, entity_id


def transcript_json_path(manifest_path: Path, activity_id: str) -> Path | None:
    entity_type, entity_id = entity_from_manifest(manifest_path)
    plural_type = "deals" if entity_type == "deal" else "leads"
    transcripts_dir = PROJECT_ROOT / "reports" / "rop_assistant" / plural_type / f"{entity_type}_{entity_id}" / "transcripts"
    return next(transcripts_dir.glob(f"call_{activity_id}_*_transcript.json"), None)


def candidates() -> list[PurgeCandidate]:
    rows: list[PurgeCandidate] = []
    for root in MANIFEST_ROOTS:
        if not root.exists():
            continue
        for manifest_path in root.glob("*_call_audio_manifest.json"):
            manifest = load_json(manifest_path)
            for call in manifest.get("calls") or []:
                if not isinstance(call, dict):
                    continue
                activity_id = str(call.get("activity_id") or "")
                transcript_path = transcript_json_path(manifest_path, activity_id)
                if not activity_id or transcript_path is None:
                    continue
                for download in call.get("downloads") or []:
                    if not isinstance(download, dict) or not download.get("local_path"):
                        continue
                    audio_path = Path(str(download["local_path"]))
                    if audio_path.exists():
                        rows.append(PurgeCandidate(manifest_path, activity_id, audio_path, transcript_path))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete Bitrix audio only when its transcript bundle exists")
    parser.add_argument("--execute", action="store_true", help="Actually delete eligible audio files")
    args = parser.parse_args()

    rows = candidates()
    total_bytes = sum(row.audio_path.stat().st_size for row in rows)
    print(f"Eligible files: {len(rows)}; disk space: {total_bytes} bytes")
    if not args.execute:
        print("Dry run only. Re-run with --execute to delete files.")
        return

    purged = 0
    for row in rows:
        row.audio_path.unlink()
        saved = record_transcribed_and_purged(
            row.manifest_path,
            row.audio_path,
            row.activity_id,
            {
                "txt_path": str(row.transcript_json_path.with_suffix(".txt")),
                "md_path": str(row.transcript_json_path.with_suffix(".md")),
                "json_path": str(row.transcript_json_path),
            },
        )
        if not saved:
            raise RuntimeError(f"Could not update manifest after deleting {row.audio_path}")
        purged += 1
    print(f"Purged files: {purged}")


if __name__ == "__main__":
    main()
