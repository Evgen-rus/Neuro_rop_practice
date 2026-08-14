"""Safely reset local deal analysis and manager-assistant history.

Dry-run is the default. ``--apply`` creates a SQLite backup, moves every deal
``analysis`` directory into a dated quarantine, and only then purges derived
SQLite state in one transaction. CRM source exports, media, and transcripts are
never selected by this tool.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from setup import MSK_TZ
from storage.rop_db import (
    DEFAULT_DB_PATH,
    deal_analysis_purge_counts,
    purge_local_deal_analysis_state,
)


DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "reports" / "rop_assistant"
PRESERVED_SECTIONS = ("raw", "history", "audio", "transcripts", "diagnostics")


@dataclass(frozen=True)
class FileInventory:
    directories: int
    files: int
    bytes: int


def analysis_directories(reports_root: Path) -> list[Path]:
    deals_root = reports_root / "deals"
    if not deals_root.exists():
        return []
    return sorted(
        path
        for path in deals_root.glob("deal_*/analysis")
        if path.is_dir()
    )


def inventory_directories(paths: list[Path]) -> FileInventory:
    files = [file for path in paths for file in path.rglob("*") if file.is_file()]
    return FileInventory(
        directories=len(paths),
        files=len(files),
        bytes=sum(file.stat().st_size for file in files),
    )


def preserved_inventory(reports_root: Path) -> dict[str, FileInventory]:
    deals_root = reports_root / "deals"
    result: dict[str, FileInventory] = {}
    for section in PRESERVED_SECTIONS:
        paths = sorted(
            path
            for path in deals_root.glob(f"deal_*/{section}")
            if path.is_dir()
        ) if deals_root.exists() else []
        result[section] = inventory_directories(paths)
    return result


def backup_sqlite(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=False)
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def restore_sqlite(backup_path: Path, destination_path: Path) -> None:
    backup = sqlite3.connect(backup_path)
    destination = sqlite3.connect(destination_path)
    try:
        backup.backup(destination)
    finally:
        destination.close()
        backup.close()


def quarantine_analysis_directories(
    reports_root: Path,
    directories: list[Path],
    quarantine_root: Path,
) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    try:
        for source in directories:
            relative = source.relative_to(reports_root)
            destination = quarantine_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(f"Quarantine destination already exists: {destination}")
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
    except Exception:
        restore_analysis_directories(moved)
        raise
    return moved


def restore_analysis_directories(moved: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moved):
        if not destination.exists():
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            raise FileExistsError(f"Cannot restore analysis directory over existing path: {source}")
        shutil.move(str(destination), str(source))


def print_preview(db_counts: dict[str, int], files: FileInventory) -> None:
    print("Локальная очистка анализов сделок — предварительный просмотр")
    print(f"Каталоги analysis: {files.directories}")
    print(f"Файлы analysis: {files.files}")
    print(f"Размер analysis: {files.bytes} байт")
    print("Записи SQLite:")
    for name, count in db_counts.items():
        print(f"  {name}: {count}")


def apply_purge(db_path: Path, reports_root: Path) -> tuple[Path, dict[str, int]]:
    timestamp = datetime.now(MSK_TZ).strftime("%Y%m%d-%H%M%S")
    quarantine_root = reports_root / "purge_backups" / timestamp
    backup_path = quarantine_root / "rop_assistant.sqlite"
    directories = analysis_directories(reports_root)
    preserved_before = preserved_inventory(reports_root)

    backup_sqlite(db_path, backup_path)
    moved = quarantine_analysis_directories(reports_root, directories, quarantine_root)
    try:
        deleted = purge_local_deal_analysis_state(db_path)
        remaining = deal_analysis_purge_counts(db_path)
        nonzero = {name: count for name, count in remaining.items() if count}
        if nonzero:
            raise RuntimeError(f"Очистка SQLite неполная: {nonzero}")
        if analysis_directories(reports_root):
            raise RuntimeError("После очистки остались активные deal analysis-каталоги")
        if preserved_inventory(reports_root) != preserved_before:
            raise RuntimeError("Изменились сохраняемые CRM/media/transcript-каталоги")
    except Exception:
        restore_sqlite(backup_path, db_path)
        restore_analysis_directories(moved)
        raise
    return quarantine_root, deleted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or reset local deal analysis without touching CRM source files, audio, or transcripts"
    )
    parser.add_argument("--apply", action="store_true", help="Create a backup and apply the purge")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--reports-root", type=Path, default=DEFAULT_REPORTS_ROOT, help=argparse.SUPPRESS)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    db_path = args.db_path.resolve()
    reports_root = args.reports_root.resolve()
    if not db_path.is_file():
        raise SystemExit(f"SQLite database not found: {db_path}")

    db_counts = deal_analysis_purge_counts(db_path)
    files = inventory_directories(analysis_directories(reports_root))
    print_preview(db_counts, files)
    if not args.apply:
        print("Dry run: данные не изменены. Для применения используйте --apply.")
        return

    quarantine_root, deleted = apply_purge(db_path, reports_root)
    print(f"Очистка завершена. Удалено записей SQLite: {sum(deleted.values())}")
    print(f"Резервная копия и analysis-карантин: {quarantine_root}")


if __name__ == "__main__":
    main()
