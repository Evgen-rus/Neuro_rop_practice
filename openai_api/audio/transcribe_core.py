"""
Общая логика транскрибации аудиофайлов для переиспользования (бот/CLI).
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai_api.audio.audio_handler import transcribe_voice
from openai_api.config import TRANSCRIPTION_MODEL, USD_RUB_RATE, logger
from openai_api.logging_utils import log_model_file_payload
from openai_api.pricing import estimate_transcription_cost

PROJECT_ROOT = Path(__file__).resolve().parent

# Жёсткий лимит модели по длительности (по сообщению от OpenAI)
MODEL_MAX_SECONDS = 1400

# Безопасная длина одного куска: 7 минут (420 секунд), уменьшаем для надёжности
SAFE_CHUNK_SECONDS = 420

# Небольшое перекрытие сегментов, чтобы сохранить связность на стыках (в секундах)
CHUNK_OVERLAP_SECONDS = 5


async def transcribe_file_async(
    filepath: str,
    max_segment_concurrency: int = 1,
    chunk_overlap_seconds: int = CHUNK_OVERLAP_SECONDS,
) -> str:
    """
    Асинхронно транскрибирует аудиофайл.

    Если файл по длительности больше безопасного лимита, он автоматически
    режется на части и отправляется в OpenAI по кускам. Куски могут
    обрабатываться параллельно (ограничено max_segment_concurrency), но
    в итоговом тексте порядок сохраняется.
    """
    logger.info(f"Открываю файл для транскрибации: {filepath}")
    log_model_file_payload(
        logger,
        title="source audio selected for transcription",
        model=TRANSCRIPTION_MODEL,
        path=filepath,
        metadata={"stage": "before_ffmpeg_conversion"},
        preview_text=False,
    )

    base_name = os.path.splitext(os.path.basename(filepath))[0]

    # 1. Конвертируем исходный файл в WAV (16 кГц, моно) с помощью ffmpeg
    #    Это нужно, чтобы дальше удобно резать аудио по сэмплам через soundfile.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=PROJECT_ROOT) as tmp_wav:
        tmp_wav_path = tmp_wav.name

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",  # перезаписать, если файл уже существует
        "-i",
        filepath,
        "-ac",
        "1",  # моно
        "-ar",
        "16000",  # частота дискретизации 16 кГц
        "-acodec",
        "pcm_s16le",  # 16-бит PCM
        tmp_wav_path,
    ]

    logger.info(f"Конвертирую аудио в WAV через ffmpeg: {' '.join(ffmpeg_cmd)}")
    try:
        subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка при конвертации аудио через ffmpeg: {e}")
        raise RuntimeError(
            "Не удалось конвертировать аудио через ffmpeg. "
            "Убедитесь, что ffmpeg установлен и доступен в PATH."
        ) from e

    try:
        # 2. Читаем сконвертированный WAV через soundfile
        data, samplerate = sf.read(tmp_wav_path, dtype="int16")
    except Exception as e:
        logger.error(f"Ошибка при чтении сконвертированного WAV: {e}")
        # В любом случае удалим временный файл
        try:
            os.remove(tmp_wav_path)
        except Exception:
            pass
        raise

    # Удаляем временный файл после чтения, он больше не нужен
    try:
        os.remove(tmp_wav_path)
    except Exception:
        # Не критично, если удалить не удалось
        pass

    # Приводим данные к numpy-массиву (на случай, если это уже не ndarray)
    data = np.asarray(data)

    total_samples = data.shape[0]
    duration_sec = total_samples / float(samplerate)
    logger.info(f"Длительность аудио после конвертации: {duration_sec:.2f} секунд")

    samples_per_chunk = SAFE_CHUNK_SECONDS * samplerate
    overlap_samples = max(0, int(chunk_overlap_seconds * samplerate))

    if duration_sec <= SAFE_CHUNK_SECONDS:
        logger.info("Аудио короче безопасного лимита, отправляю одним куском")

    texts: list[tuple[int, str]] = []

    # Считаем количество сегментов
    total_chunks = max(1, (total_samples + samples_per_chunk - 1) // samples_per_chunk)
    semaphore = asyncio.Semaphore(max(1, max_segment_concurrency))

    async def process_chunk(idx: int, start_sample: int, end_sample: int) -> None:
        chunk_data = data[start_sample:end_sample]
        if chunk_data.size == 0:
            logger.warning(f"Сегмент {idx + 1} пустой, пропускаю")
            return

        chunk_duration_sec = (end_sample - start_sample) / float(samplerate)
        start_time_sec = start_sample / float(samplerate)
        end_time_sec = end_sample / float(samplerate)

        logger.info(
            f"Готовлю сегмент {idx + 1}/{total_chunks}: "
            f"{start_time_sec:.1f}–{end_time_sec:.1f} сек "
            f"({chunk_duration_sec:.1f} сек)"
        )

        # 3. Пишем сегмент во временный WAV в память (байтовый поток)
        buffer = io.BytesIO()
        sf.write(buffer, chunk_data, samplerate, format="WAV", subtype="PCM_16")
        buffer.seek(0)
        wav_bytes = buffer.read()

        if not wav_bytes:
            logger.warning(f"Сегмент {idx + 1} пустой после записи в WAV, пропускаю")
            return

        segment_file_name = f"{base_name}_part_{idx + 1}.wav"
        logger.info(
            f"Отправляю сегмент {idx + 1}/{total_chunks} в модель: {segment_file_name}"
        )

        try:
            async with semaphore:
                segment_text = await transcribe_voice(
                    wav_bytes,
                    file_name=segment_file_name,
                    language="ru",
                )
        except Exception as e:  # noqa: BLE001 — хотим залогировать и продолжить другие сегменты
            logger.error(f"Ошибка при транскрибации сегмента {idx + 1}: {e}")
            return

        header = (
            f"[Сегмент {idx + 1}/{total_chunks} "
            f"({start_time_sec:.1f}–{end_time_sec:.1f} сек)]"
        )
        texts.append((idx, f"{header}\n{segment_text}"))

    tasks = []
    for idx in range(total_chunks):
        start_sample = max(0, idx * samples_per_chunk - overlap_samples if idx > 0 else 0)
        end_sample = min(start_sample + samples_per_chunk, total_samples)
        tasks.append(asyncio.create_task(process_chunk(idx, start_sample, end_sample)))

    # Дожидаемся всех сегментов
    if tasks:
        await asyncio.gather(*tasks)

    if len(texts) != total_chunks:
        logger.error(
            "Получено сегментов: %s из %s. Некоторых сегментов нет в ответе.",
            len(texts),
            total_chunks,
        )
        raise ValueError("Не удалось получить текст всех сегментов")

    if not texts:
        raise ValueError("Не удалось получить текст ни из одного сегмента")

    # Сохраняем порядок согласно исходной нумерации сегментов
    texts.sort(key=lambda item: item[0])
    full_text = "\n\n".join([item[1] for item in texts])
    return full_text


def get_audio_duration_seconds(filepath: str | Path) -> float | None:
    """
    Returns audio duration using ffprobe when available.
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(filepath),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def estimate_transcription_cost_usd(model: str, duration_seconds: float | None) -> float | None:
    cost = estimate_transcription_cost(model, duration_seconds, USD_RUB_RATE)
    return cost.get("estimated_cost_usd")


def estimate_transcription_cost_details(model: str, duration_seconds: float | None) -> dict:
    return estimate_transcription_cost(model, duration_seconds, USD_RUB_RATE)


def save_transcription(text: str, original_filepath: str) -> str:
    """
    Сохраняет транскрибацию в .txt-файл рядом с исходным аудио.

    Имя файла: <имя_аудио>_transcription_YYYY-MM-DD_HH-MM-SS.txt
    Возвращает путь к созданному файлу.
    """
    base_dir = os.path.dirname(original_filepath)
    base_name = os.path.splitext(os.path.basename(original_filepath))[0]

    # Дата и время в имени файла, чтобы файлы не перезаписывались
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    txt_name = f"{base_name}_transcription_{timestamp}.txt"
    txt_path = os.path.join(base_dir, txt_name)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    logger.info(f"Транскрипция сохранена в файл: {txt_path}")
    return txt_path


def save_transcription_bundle(
    text: str,
    original_filepath: str,
    output_dir: str | Path,
    stem: str,
    metadata: dict | None = None,
) -> dict:
    """
    Saves a transcript bundle for deal-oriented manual processing.

    Files:
    - <stem>.txt: plain transcript;
    - <stem>.md: readable transcript with metadata;
    - <stem>.json: machine-readable metadata and transcript text.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    txt_path = output_path / f"{stem}.txt"
    md_path = output_path / f"{stem}.md"
    json_path = output_path / f"{stem}.json"

    created_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "created_at": created_at,
        "original_audio_path": str(original_filepath),
        "transcript_txt_path": str(txt_path),
        "transcript_md_path": str(md_path),
        "metadata": metadata or {},
        "text": text,
    }

    txt_path.write_text(text, encoding="utf-8")

    metadata_lines = [
        "# Транскрибация звонка",
        "",
        f"- Создано: {created_at}",
        f"- Аудио: `{original_filepath}`",
    ]
    for key, value in (metadata or {}).items():
        if value not in (None, "", [], {}):
            metadata_lines.append(f"- {key}: {value}")

    md_path.write_text("\n".join(metadata_lines) + "\n\n## Текст\n\n" + text + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("Транскрибация сохранена: %s", md_path)
    return {
        "txt_path": str(txt_path),
        "md_path": str(md_path),
        "json_path": str(json_path),
    }

