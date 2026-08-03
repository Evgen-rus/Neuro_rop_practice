"""
Модуль для обработки голосовых сообщений и их преобразования в текст.
"""

import sys
from pathlib import Path
from openai import AsyncOpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai_api.config import OPENAI_API_KEY, TRANSCRIPTION_MODEL, logger
from reliability.retry import DEFAULT_TRANSPORT_RETRY, RetryCallback, run_with_retry_async

# Инициализация клиента OpenAI
client = AsyncOpenAI(api_key=OPENAI_API_KEY, max_retries=0)

async def transcribe_voice(
    voice_data: bytes,
    file_name: str = "voice.ogg",
    language: str = "ru",
    retry_callback: RetryCallback | None = None,
) -> str:
    """
    Асинхронная функция для транскрибации голосового сообщения в текст.
    
    Args:
        voice_data: Байтовые данные голосового сообщения
        file_name: Имя файла для отправки в API
        language: Язык голосового сообщения для лучшего распознавания
        
    Returns:
        str: Распознанный текст
        
    Raises:
        Exception: В случае ошибки при транскрибации
    """
    try:
        # Browser recordings are handled in memory. Never log audio size,
        # hashes, bytes, file content, or transcript content.
        logger.info("Начинаю транскрибацию голосового сообщения")
        
        # Отправляем запрос на транскрибацию
        transcript = await run_with_retry_async(
            lambda: client.audio.transcriptions.create(
                model=TRANSCRIPTION_MODEL,
                file=(file_name, voice_data),
                language=language,
            ),
            operation_name=f"openai:audio.transcriptions.create:{file_name}",
            policy=DEFAULT_TRANSPORT_RETRY,
            on_event=retry_callback,
        )
        
        # Return the transcript to the caller; do not log its content.
        text = transcript.text
        logger.info("Голосовое сообщение успешно транскрибировано")
        
        return text
        
    except Exception as e:
        # Если произошла ошибка с моделью, пробуем запасную модель
        if "invalid model ID" in str(e):
            logger.warning("Основная модель транскрибации недоступна, используем запасную")
            try:
                transcript = await run_with_retry_async(
                    lambda: client.audio.transcriptions.create(
                        model="whisper-1",  # Запасная модель
                        file=(file_name, voice_data),
                        language=language,
                    ),
                    operation_name=f"openai:audio.transcriptions.create:whisper-1:{file_name}",
                    policy=DEFAULT_TRANSPORT_RETRY,
                    on_event=retry_callback,
                )
                text = transcript.text
                logger.info("Голосовое сообщение транскрибировано запасной моделью")
                return text
                
            except Exception as inner_e:
                logger.error("Ошибка при использовании запасной модели: %s", inner_e.__class__.__name__)
                raise
        else:
            logger.error("Ошибка при транскрибации: %s", e.__class__.__name__)
            raise 
