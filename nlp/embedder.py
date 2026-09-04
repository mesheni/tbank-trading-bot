"""Модель представлений новостей: эмбеддинги предложений.

Использует sentence-transformers (transformers+torch) при наличии; если
зависимости не установлены, возвращает None — пайплайн продолжает работать
без кластеризации (тональность считается лексиконом/трансформером отдельно).
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)


class NewsEmbedder:
    """Обёртка над sentence-transformers с e5-моделью по умолчанию."""

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small"):
        self.model_name = model_name
        self._model = None
        self.available = self._try_load(model_name)

    def _try_load(self, model_name: str) -> bool:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            log.info(
                "sentence-transformers не установлен — кластеризация повестки отключена. "
                "Для включения: pip install sentence-transformers torch"
            )
            return False
        try:
            self._model = SentenceTransformer(model_name)
            log.info("Эмбеддер загружен: %s", model_name)
            return True
        except Exception as exc:  # сеть/модель недоступны
            log.warning("Не удалось загрузить эмбеддер %s: %s", model_name, exc)
            return False

    def encode(self, texts: list[str]) -> np.ndarray | None:
        """Матрица [n_texts, dim] L2-нормированная; None, если эмбеддер недоступен."""
        if not self.available or self._model is None or not texts:
            return None
        # у e5 обязательны префиксы задачи
        prefix = "query: " if "e5" in self.model_name.lower() else ""
        return self._model.encode([prefix + t for t in texts], normalize_embeddings=True)
