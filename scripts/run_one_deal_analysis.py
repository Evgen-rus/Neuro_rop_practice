"""One deal: FULL or incremental, with live terminal output.

Does not duplicate Bitrix/LLM logic. It only asks for deal/mode, checks
incremental baseline, then runs existing run_rop_assistant.py.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai_api.llm.analyze_deal import COMPATIBLE_DEAL_PROMPT_VERSIONS
from openai_api.llm.trusted_baseline import get_trusted_deal_baseline
from setup import configure_console
from storage.rop_db import DEFAULT_DB_PATH, get_deal_control_deal

LOGIC_VERSION = "change-aware-v1"
MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"


def python_executable() -> str:
    venv_python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Один запуск анализа сделки: FULL или incremental, лог в этом терминале.",
    )
    parser.add_argument("--deal-id", help="ID сделки Bitrix")
    parser.add_argument(
        "--mode",
        choices=(MODE_FULL, MODE_INCREMENTAL),
        help="full — принудительный полный анализ; incremental — только если есть trusted baseline",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Без выбора ID и режима в терминале. Нужны --deal-id и --mode.",
    )
    return parser.parse_args(argv)


def normalize_mode(raw: str) -> str | None:
    value = str(raw or "").strip().casefold()
    if value in {"1", "full", "полный", "f"}:
        return MODE_FULL
    if value in {"2", "incremental", "инкрементальный", "i"}:
        return MODE_INCREMENTAL
    return None


def prompt_nonempty(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("Нужно непустое значение.")


def prompt_mode() -> str:
    print("Режим:")
    print("  1) full — полный анализ (--force-llm)")
    print("  2) incremental — инкрементальный (без тихого перехода в FULL)")
    while True:
        parsed = normalize_mode(input("Выберите 1 или 2: "))
        if parsed:
            return parsed
        print("Введите 1/full или 2/incremental.")


def resolve_choice(args: argparse.Namespace) -> tuple[str, str]:
    deal_id = str(args.deal_id or "").strip()
    mode = normalize_mode(args.mode) if args.mode else None
    if args.yes:
        if not deal_id or mode is None:
            raise SystemExit("--yes требует --deal-id и --mode full|incremental")
        return deal_id, mode
    if not deal_id:
        deal_id = prompt_nonempty("ID сделки")
    if mode is None:
        mode = prompt_mode()
    return deal_id, mode


def describe_deal(db_path: str | Path, deal_id: str) -> str:
    row = get_deal_control_deal(db_path, deal_id=deal_id)
    if not row:
        return f"сделка {deal_id} (в локальном Контроле пока нет карточки — пайплайн всё равно пойдёт в Bitrix)"
    title = str(row.get("title") or "").strip() or "без названия"
    stage = str(row.get("stage_name") or row.get("stage_id") or "").strip()
    extra = f", стадия {stage}" if stage else ""
    return f"сделка {deal_id} — {title}{extra}"


def incremental_block_reason(db_path: str | Path, deal_id: str) -> str | None:
    baseline = get_trusted_deal_baseline(
        db_path,
        deal_id,
        compatible_prompt_versions=COMPATIBLE_DEAL_PROMPT_VERSIONS,
        expected_logic_version=LOGIC_VERSION,
    )
    if baseline is None:
        return (
            "Incremental нельзя: нет доверенного FULL/incremental baseline. "
            "Сначала запусти --mode full. Тихий FULL отсюда не стартую."
        )
    return None


def assistant_command(deal_id: str, mode: str) -> list[str]:
    command = [
        python_executable(),
        str(PROJECT_ROOT / "run_rop_assistant.py"),
        "--yes",
        "--entity",
        "deal",
        "--ids",
        deal_id,
        "--transcript",
        "all",
    ]
    if mode == MODE_INCREMENTAL:
        command.append("--no-force-llm")
    return command


def assistant_env(mode: str) -> dict[str, str]:
    env = os.environ.copy()
    if mode == MODE_INCREMENTAL:
        env["DEAL_INCREMENTAL_ANALYSIS_ENABLED"] = "true"
        env["DAYTIME_CYCLE_ENABLED"] = env.get("DAYTIME_CYCLE_ENABLED") or "false"
    return env


def print_plan(deal_label: str, mode: str) -> None:
    print("")
    print("=== Один запуск анализа сделки ===")
    print(deal_label)
    if mode == MODE_FULL:
        print("Режим: FULL (принудительный полный LLM)")
        print("Incremental-флаг для этого запуска не включаю: --force-llm всегда FULL.")
    else:
        print("Режим: incremental")
        print("Force-llm выключен. Если change detection скажет skip — в логе будет skip, без LLM.")
        print("Если incremental упадёт в запасной FULL, это будет видно в логе, не скрываю.")
    print("Дальше живой вывод существующего пайплайна: CRM → аудио → транскрипт → анализ.")
    print("")


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv)
    deal_id, mode = resolve_choice(args)
    db_path = args.db_path
    print(describe_deal(db_path, deal_id))
    if mode == MODE_INCREMENTAL:
        blocked = incremental_block_reason(db_path, deal_id)
        if blocked:
            print(blocked)
            return 2
    print_plan(describe_deal(db_path, deal_id), mode)
    completed = subprocess.run(
        assistant_command(deal_id, mode),
        cwd=str(PROJECT_ROOT),
        env=assistant_env(mode),
        check=False,
    )
    print("")
    if completed.returncode == 0:
        print(f"=== Готово: сделка {deal_id}, режим {mode}, код 0 ===")
    else:
        print(f"=== Пайплайн завершился с кодом {completed.returncode} (сделка {deal_id}, режим {mode}) ===")
    return int(completed.returncode)


if __name__ == "__main__":
    sys.exit(main())
