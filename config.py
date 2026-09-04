"""Конфигурация бота: значения берутся из .env / переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    # --- API ---
    token: str = field(default_factory=lambda: os.getenv("T_INVEST_TOKEN", ""))
    mode: str = field(default_factory=lambda: os.getenv("MODE", "sandbox").lower())

    # --- Инструменты ---
    tickers: list[str] = field(default_factory=lambda: _env_list("TICKERS", ["SBER", "GAZP", "LKOH"]))
    candle_interval: str = field(default_factory=lambda: os.getenv("CANDLE_INTERVAL", "hour").lower())

    # --- Модель ---
    forecast_horizon: int = field(default_factory=lambda: _env_int("FORECAST_HORIZON", 1))
    history_days: int = field(default_factory=lambda: _env_int("HISTORY_DAYS", 720))

    # --- Риск-менеджмент ---
    max_position_pct: float = field(default_factory=lambda: _env_float("MAX_POSITION_PCT", 0.25))
    stop_loss_pct: float = field(default_factory=lambda: _env_float("STOP_LOSS_PCT", 0.03))
    take_profit_pct: float = field(default_factory=lambda: _env_float("TAKE_PROFIT_PCT", 0.05))
    min_abs_return: float = field(default_factory=lambda: _env_float("MIN_ABS_RETURN", 0.004))
    news_sentiment_gate: float = field(default_factory=lambda: _env_float("NEWS_SENTIMENT_GATE", -0.35))

    # --- Издержки (для бэктеста) ---
    commission_pct: float = field(default_factory=lambda: _env_float("COMMISSION_PCT", 0.0004))
    slippage_pct: float = field(default_factory=lambda: _env_float("SLIPPAGE_PCT", 0.0002))

    # --- Бот ---
    loop_interval_sec: int = field(default_factory=lambda: _env_int("LOOP_INTERVAL_SEC", 300))
    sandbox_initial_rub: float = field(default_factory=lambda: _env_float("SANDBOX_INITIAL_RUB", 1_000_000))

    # --- Пути ---
    db_path: Path = field(default_factory=lambda: BASE_DIR / os.getenv("DB_PATH", "data/market.sqlite"))
    models_dir: Path = field(default_factory=lambda: BASE_DIR / os.getenv("MODELS_DIR", "models_artifacts"))
    reports_dir: Path = field(default_factory=lambda: BASE_DIR / os.getenv("REPORTS_DIR", "reports"))

    # --- NLP ---
    embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
    )
    sentiment_model: str = field(
        default_factory=lambda: os.getenv("SENTIMENT_MODEL", "Blanchefort/rubert-base-cased-sentiment")
    )
    news_half_life_hours: float = field(default_factory=lambda: _env_float("NEWS_HALF_LIFE_HOURS", 24.0))
    # Тональность: auto (трансформер, если установлен torch+transformers, иначе лексикон) |
    # lexicon (принудительно офлайн) | transformer (паднуть, если трансформер недоступен)
    nlp_sentiment: str = field(default_factory=lambda: os.getenv("NLP_SENTIMENT", "auto").lower())
    # Эмбеддеры повестки: 0 — отключить (экономит ~450 МБ RAM, кластеризация тем off)
    nlp_embedder: bool = field(
        default_factory=lambda: os.getenv("NLP_EMBEDDER", "1").strip().lower()
        not in {"0", "false", "off", "no"}
    )

    @property
    def is_sandbox(self) -> bool:
        return self.mode == "sandbox"

    def validate(self) -> None:
        if self.mode not in {"sandbox", "real"}:
            raise ValueError(f"MODE должен быть sandbox|real, получено: {self.mode}")
        if not self.token:
            raise ValueError(
                "T_INVEST_TOKEN не задан. Получите токен на https://www.tbank.ru/invest/open-api "
                "и укажите его в файле .env"
            )
        for name in ("max_position_pct", "stop_loss_pct", "take_profit_pct"):
            value = getattr(self, name)
            if not 0 < value < 1:
                raise ValueError(f"{name} должен быть в (0; 1), получено: {value}")


# Интервалы свечей: имя -> значение enum T-Invest API и максимальная глубина одного запроса
CANDLE_INTERVALS: dict[str, dict] = {
    "1min": {"enum": "CANDLE_INTERVAL_1_MIN", "chunk": 1},
    "5min": {"enum": "CANDLE_INTERVAL_5_MIN", "chunk": 7},
    "15min": {"enum": "CANDLE_INTERVAL_15_MIN", "chunk": 14},
    "hour": {"enum": "CANDLE_INTERVAL_HOUR", "chunk": 30},
    "day": {"enum": "CANDLE_INTERVAL_DAY", "chunk": 365},
}

# Часовой пояс MOEX (MSK, UTC+3)
MSK = __import__("datetime").timezone(__import__("datetime").timedelta(hours=3))
