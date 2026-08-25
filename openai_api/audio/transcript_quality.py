"""Deterministic transcript quality signals used before paid deal analysis."""

from __future__ import annotations

import re
from typing import Any


NO_CONTACT_TRANSCRIPT_MARKERS = (
    "абонент недоступен",
    "временно недоступен",
    "не может ответить",
    "оставьте сообщение",
    "после звукового сигнала",
    "голосовая почта",
    "автоответчик",
)

SHORT_BUSINESS_SIGNAL_MARKERS = (
    "соглас",
    "берём",
    "берем",
    "оплат",
    "договор",
    "счёт",
    "счет",
    "отказ",
    "дорого",
    "цена",
    "срок",
    "готов",
)


def normalized_transcript_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def is_meaningful_transcript(value: Any) -> bool:
    """Reject empty/service audio while retaining short substantive voice notes."""
    text = normalized_transcript_text(value)
    if any(marker in text for marker in NO_CONTACT_TRANSCRIPT_MARKERS):
        return False
    words = re.findall(r"[a-zа-яё0-9]+", text, flags=re.I)
    letters_and_digits = re.sub(r"[^a-zа-яё0-9]+", "", text, flags=re.I)
    if len(words) >= 2 and any(marker in text for marker in SHORT_BUSINESS_SIGNAL_MARKERS):
        return True
    return len(words) >= 3 and len(letters_and_digits) >= 20
