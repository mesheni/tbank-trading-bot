"""Тональность новостей.

Приоритет: трансформер (Blanchefort/rubert-base-cased-sentiment) при наличии
transformers/torch. Фолбэк — русский финансовый лексикон, работающий offline.
Скор: [-1, 1], где +1 — позитив, -1 — негатив.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

RU_POSITIVE = {
    "рост", "вырос", "выросли", "подорожал", "прибыль", "прибыли", "рекорд", "рекордный",
    "дивиденды", "дивиденд", "выплаты", "повысил", "повысили", "повышение", "прогноз повышен",
    "оптимизм", "обновил максимум", "обновила максимум", "драйвер", "успешн", "выиграл",
    "выиграла", "контракт", "подписал", "подписала", "расширение", "запуск", "запустил",
    "выкупка", "байбэк", "байбек", "упрочил", "укрепил", "укрепилась", "прирастил", "превысил",
    "превзошла", "превзошёл", "положительн", "продажи выросли", "выручка выросла", "выручка увеличилась",
    "снизил долг", "снизила долг", "повысил рейтинг", "повысила рейтинг", "одобрил", "поддержал",
    "инвестпрограмм", "модернизаци", "эффективность выросла", "наградил", "премия", "партнерство",
}

RU_NEGATIVE = {
    "падение", "упал", "упала", "упали", "подешевел", "убыток", "убытки", "убыточн",
    "снизил", "снизили", "снижение", "понизил", "понижен", "прогноз понижен", "пессимизм",
    "обвал", "обрушился", "просел", "просела", "проиграл", "проиграла", "потерял", "потеряла",
    "штраф", "арест", "арестован", "арестованы", "санкции", "санкци", "риск", "риски",
    "расследование", "иск", "иском", "суд", "судебн", "долг", "дефолт", "банкрот",
    "банкротств", "отзыв лицензии", "отозвал лицензию", "приостанов", "остановк", "перебои",
    "авария", "инцидент", "сократил", "сокращение", "увольн", "забастовк", "дефицит",
    "инфляция", "ставка повышена", "негативн", "отрицательн", "выручка снизилась", "продажи упали",
    "отложил", "перенесл", "отменил", "отмена", "неустойка", "конфликт", "рейтинг понижен",
}

_WORD_RE = re.compile(r"[а-яёa-z]+", re.IGNORECASE)


class LexiconSentiment:
    """Лексиконная тональность: доля позитив/негатив корней во всём тексте."""

    name = "lexicon"

    def score(self, text: str) -> float:
        if not text:
            return 0.0
        words = _WORD_RE.findall(text.lower())
        if not words:
            return 0.0
        joined = " ".join(words)
        pos_hits = sum(1 for w in RU_POSITIVE if w in joined)
        neg_hits = sum(1 for w in RU_NEGATIVE if w in joined)
        total = pos_hits + neg_hits
        if total == 0:
            return 0.0
        # насыщение: 5+ совпадений дают максимальный по знаку вес
        strength = min(1.0, total / 5.0)
        return strength * (pos_hits - neg_hits) / total


class TransformersSentiment:
    """Трёхклассовый трансформер тональности (NEGATIVE/NEUTRAL/POSITIVE и вариации)."""

    name = "transformers"

    def __init__(self, model_name: str):
        from transformers import pipeline

        self._pipeline = pipeline("text-classification", model=model_name, truncation=True, max_length=256)
        self._label_map = {}
        for label in self._pipeline.model.config.id2label.values():
            normalized = str(label).upper()
            if "NEG" in normalized or normalized in {"LABEL_0", "0"}:
                self._label_map[label] = -1.0
            elif "POS" in normalized or normalized in {"LABEL_2", "LABEL_1", "2", "1"}:
                self._label_map[label] = 1.0
            else:
                self._label_map[label] = 0.0

    def score(self, text: str) -> float:
        text = (text or "").strip()
        if not text:
            return 0.0
        result = self._pipeline(text[:1000], top_k=1)
        if not result:
            return 0.0
        label = result[0]["label"]
        prob = float(result[0].get("score", 0.0))
        return self._label_map.get(label, 0.0) * prob


def make_sentiment(
    model_name: str = "Blanchefort/rubert-base-cased-sentiment",
    preference: str = "auto",
):
    """Фабрика тональности.

    preference: auto — трансформер, если доступен, иначе лексикон;
    lexicon — принудительно офлайн-лексикон (для слабых по RAM серверов);
    transformer — только трансформер (ошибка, если недоступен).
    """
    if preference == "lexicon":
        log.info("Тональность: лексикон (NLP_SENTIMENT=lexicon)")
        return LexiconSentiment()
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401

        sentiment = TransformersSentiment(model_name)
        log.info("Тональность: трансформер %s", model_name)
        return sentiment
    except Exception as exc:
        if preference == "transformer":
            raise
        log.info(
            "Трансформер тональности недоступен (%s) — используется лексиконный фолбэк. "
            "Для включения: pip install transformers torch",
            str(exc)[:120],
        )
        return LexiconSentiment()
